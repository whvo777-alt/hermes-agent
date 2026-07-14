"""Phase 15F tests — governed runtime invocation contract."""

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
    BOUNDARY_RESERVED,
    prepare_production_runtime_boundary,
)
from agent.coo.production_runtime_invocation import (
    ACTION_RESERVE_RUNTIME_INVOCATION,
    BLOCK_CONTROLLED_WINDOW_CLOSED,
    BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED,
    BLOCK_CONTROLLED_WINDOW_EXPIRED,
    BLOCK_EMERGENCY_CLOSE_UNAVAILABLE,
    BLOCK_GOVERNED_RUNTIME_INVOCATION_CONFLICT,
    BLOCK_GOVERNED_RUNTIME_SESSION_MISSING,
    BLOCK_IDENTITY_SEPARATION_INVALID,
    BLOCK_INVOCATION_TTL_EXCEEDS_BOUNDARY,
    BLOCK_INVOCATION_TTL_INVALID,
    BLOCK_KILL_SWITCH_UNAVAILABLE,
    BLOCK_OPERATOR_IDENTITY_INVALID,
    BLOCK_PERMISSION_EXECUTOR_MISMATCH,
    BLOCK_PRODUCTION_EXECUTION_ENABLED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_RUNTIME_ALREADY_INVOKED,
    BLOCK_RUNTIME_BOUNDARY_EXECUTOR_MISMATCH,
    BLOCK_RUNTIME_BOUNDARY_EXPIRED,
    BLOCK_RUNTIME_BOUNDARY_ID_MISMATCH,
    BLOCK_RUNTIME_BOUNDARY_MISSING,
    BLOCK_RUNTIME_FACTORY_UNAVAILABLE,
    BLOCK_RUNTIME_INVOCATION_ALREADY_RESERVED,
    BLOCK_RUNTIME_INVOKER_ENABLED,
    BLOCK_RUNTIME_PERMISSION_EXPIRED,
    BLOCK_RUNTIME_PERMISSION_MISSING,
    BLOCK_SESSION_EXECUTOR_MISMATCH,
    EVENT_EXECUTION_PHRASE_REQUIRED,
    EVENT_RUNTIME_INVOCATION_BLOCKED,
    EVENT_RUNTIME_INVOCATION_RESERVED,
    INVOCATION_BLOCKED,
    INVOCATION_EXPIRED,
    INVOCATION_READY,
    INVOCATION_RESERVED,
    RELEASE_GOVERNED_RUNTIME_INVOCATION_RESERVED,
    GovernedRuntimeInvocationContext,
    ProductionRuntimeInvocationError,
    build_production_runtime_invocation_release_summary,
    evaluate_production_runtime_invocation,
    format_production_runtime_invocation_status,
    load_runtime_invocation_events,
    reserve_production_runtime_invocation,
    resolve_latest_governed_runtime_invocation_dashboard_digest,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli import test_production_runtime_boundary as _bound
from tests.hermes_cli.test_production_runtime_permission import (
    _PERMISSION_OPERATOR,
    _RUNTIME_EXECUTOR,
)
from tests.hermes_cli.test_production_controlled_window import _WINDOW_OPERATOR

_SESSION_OPERATOR = _bound._SESSION_OPERATOR
_BOUNDARY_OPERATOR = _bound._BOUNDARY_OPERATOR
_INVOCATION_OPERATOR = "invocation-operator-phase15f"


class TestProductionRuntimeInvocation(_bound.TestProductionRuntimeBoundary):
    def setUp(self) -> None:
        super().setUp()
        self.invocation_store_dir = (
            self.hermes_home / "coo" / "production-runtime-invocation"
        )
        self.invocation_store_dir.mkdir(parents=True, exist_ok=True)

    def _prepare_boundary(self, **overrides) -> tuple[str, str, str, str]:
        activation_id, permission_id, session_id = self._start_session()
        boundary = prepare_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id, permission_id, session_id, **overrides
            )
        )
        return activation_id, permission_id, session_id, boundary.boundary_id

    def _inv_kwargs(
        self,
        activation_id: str,
        permission_id: str,
        session_id: str,
        boundary_id: str,
        **overrides,
    ):
        base = {
            **self._eval_kwargs(activation_id),
            "invocation_store_dir": self.invocation_store_dir,
            "boundary_store_dir": self.boundary_store_dir,
            "permission_store_dir": self.permission_store_dir,
            "session_store_dir": self.session_store_dir,
            "executor_id": _RUNTIME_EXECUTOR,
            "operator_id": _INVOCATION_OPERATOR,
            "permission_id": permission_id,
            "session_id": session_id,
            "boundary_id": boundary_id,
            "ttl_seconds": 30,
        }
        base.update(overrides)
        return base

    def _invocation_context_kwargs(self, activation_id: str, **overrides):
        base = {
            "activation_request_id": activation_id,
            "invocation_store_dir": self.invocation_store_dir,
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

    def _invocation_correlated_digests(
        self, activation_id: str
    ) -> tuple[str, str, str, str, str, str]:
        base = self._boundary_correlated_digests(activation_id)
        boundary_path = self.boundary_store_dir / f"{activation_id}.json"
        return (*base, self._digest(boundary_path))

    # -- 1. check readiness ----------------------------------------------------

    def test_check_ready_when_boundary_reserved_session_started_permission_issued(
        self,
    ) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        summary = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id, permission_id, session_id, boundary_id, operator_id=""
            )
        )
        self.assertEqual(summary.invocation_state, INVOCATION_READY)
        self.assertTrue(summary.invocation_ready)
        self.assertTrue(summary.boundary_valid)
        self.assertEqual(
            summary.recommended_action, ACTION_RESERVE_RUNTIME_INVOCATION
        )
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.permission_consumed)
        self.assertFalse(summary.permission_revoked)
        self.assertFalse(summary.execution_phrase_verified)
        self.assertTrue(summary.execution_phrase_required)

    # -- 2. read-only digests unchanged (boundary included) ---------------------

    def test_check_read_only_digests_unchanged(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        before = self._invocation_correlated_digests(activation_id)
        evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id, permission_id, session_id, boundary_id, operator_id=""
            )
        )
        after = self._invocation_correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertFalse(list(self.invocation_store_dir.glob("*.json")))

    # -- 3. reserve success -----------------------------------------------------

    def test_reserve_success_append_only(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        before = self._invocation_correlated_digests(activation_id)
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                summary = reserve_production_runtime_invocation(
                    **self._inv_kwargs(
                        activation_id, permission_id, session_id, boundary_id
                    )
                )
        after = self._invocation_correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertEqual(summary.invocation_state, INVOCATION_RESERVED)
        self.assertTrue(summary.runtime_invocation_id)
        self.assertEqual(summary.boundary_id, boundary_id)
        self.assertTrue(summary.boundary_invocation_id)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.permission_consumed)
        self.assertFalse(summary.execution_phrase_verified)
        self.assertTrue(summary.execution_phrase_required)
        events = load_runtime_invocation_events(
            activation_id, store_dir=self.invocation_store_dir
        )
        types = [event.event_type for event in events]
        self.assertIn(EVENT_RUNTIME_INVOCATION_RESERVED, types)
        self.assertIn(EVENT_EXECUTION_PHRASE_REQUIRED, types)
        self.assertIn(EVENT_RUNTIME_INVOCATION_BLOCKED, types)
        # Phase 15E's cutover-start event must never appear here.
        self.assertNotIn("cutover_start_requested", types)

    # -- 4. missing session blocked ---------------------------------------------

    def test_missing_session_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        summary = evaluate_production_runtime_invocation(
            **self._inv_kwargs(activation_id, permission_id, "", "")
        )
        self.assertIn(
            BLOCK_GOVERNED_RUNTIME_SESSION_MISSING, summary.blocking_items
        )
        with self.assertRaises(ProductionRuntimeInvocationError):
            reserve_production_runtime_invocation(
                **self._inv_kwargs(activation_id, permission_id, "", "")
            )

    # -- 5. missing / expired permission blocked ---------------------------------

    def test_missing_and_expired_permission_blocked(self) -> None:
        activation_id = self._open_window()
        missing = evaluate_production_runtime_invocation(
            **self._inv_kwargs(activation_id, "", "", "")
        )
        self.assertIn(BLOCK_RUNTIME_PERMISSION_MISSING, missing.blocking_items)

        activation_id2, permission_id2, session_id2, boundary_id2 = (
            self._prepare_boundary()
        )
        later = self._now + timedelta(seconds=301)
        expired = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id2, permission_id2, session_id2, boundary_id2, now=later
            )
        )
        self.assertIn(BLOCK_RUNTIME_PERMISSION_EXPIRED, expired.blocking_items)
        with self.assertRaises(ProductionRuntimeInvocationError):
            reserve_production_runtime_invocation(
                **self._inv_kwargs(
                    activation_id2,
                    permission_id2,
                    session_id2,
                    boundary_id2,
                    now=later,
                )
            )

    # -- 6. missing / expired boundary blocked -----------------------------------

    def test_missing_and_expired_boundary_blocked(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        missing = evaluate_production_runtime_invocation(
            **self._inv_kwargs(activation_id, permission_id, session_id, "")
        )
        self.assertIn(BLOCK_RUNTIME_BOUNDARY_MISSING, missing.blocking_items)
        with self.assertRaises(ProductionRuntimeInvocationError):
            reserve_production_runtime_invocation(
                **self._inv_kwargs(activation_id, permission_id, session_id, "")
            )

        activation_id2, permission_id2, session_id2, boundary_id2 = (
            self._prepare_boundary(ttl_seconds=15)
        )
        later = self._now + timedelta(seconds=16)
        expired = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id2,
                permission_id2,
                session_id2,
                boundary_id2,
                now=later,
            )
        )
        self.assertIn(BLOCK_RUNTIME_BOUNDARY_EXPIRED, expired.blocking_items)
        with self.assertRaises(ProductionRuntimeInvocationError):
            reserve_production_runtime_invocation(
                **self._inv_kwargs(
                    activation_id2,
                    permission_id2,
                    session_id2,
                    boundary_id2,
                    now=later,
                )
            )

    # -- 7. boundary id mismatch blocked ------------------------------------------

    def test_boundary_id_mismatch_blocked(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        mismatch = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                "not-the-real-boundary-id",
            )
        )
        self.assertIn(
            BLOCK_RUNTIME_BOUNDARY_ID_MISMATCH, mismatch.blocking_items
        )
        with self.assertRaises(ProductionRuntimeInvocationError):
            reserve_production_runtime_invocation(
                **self._inv_kwargs(
                    activation_id,
                    permission_id,
                    session_id,
                    "not-the-real-boundary-id",
                )
            )

    # -- 8. controlled window closed / emergency / expired blocked ----------------

    def test_window_closed_emergency_expired_blocked(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                reason_code=REASON_OPERATOR_CLOSE,
            )
        )
        closed = evaluate_production_runtime_invocation(
            **self._inv_kwargs(activation_id, permission_id, session_id, boundary_id)
        )
        self.assertIn(BLOCK_CONTROLLED_WINDOW_CLOSED, closed.blocking_items)

        (
            activation_id2,
            permission_id2,
            session_id2,
            boundary_id2,
        ) = self._prepare_boundary()
        emergency_close_production_controlled_window(
            **self._eval_kwargs(
                activation_id2,
                operator_id=_WINDOW_OPERATOR,
                actor_role="operator",
                reason_code=REASON_INCIDENT_DETECTED,
            )
        )
        emergency = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id2, permission_id2, session_id2, boundary_id2
            )
        )
        self.assertIn(
            BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED, emergency.blocking_items
        )

        (
            activation_id3,
            permission_id3,
            session_id3,
            boundary_id3,
        ) = self._prepare_boundary()
        later = self._now + timedelta(hours=2)
        expired = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id3,
                permission_id3,
                session_id3,
                boundary_id3,
                now=later,
            )
        )
        self.assertTrue(expired.controlled_window_expired)
        self.assertIn(BLOCK_CONTROLLED_WINDOW_EXPIRED, expired.blocking_items)

    # -- 9. TTL bounds -------------------------------------------------------------

    def test_ttl_bounds_low_and_high_blocked(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        low = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id, permission_id, session_id, boundary_id, ttl_seconds=9
            )
        )
        self.assertIn(BLOCK_INVOCATION_TTL_INVALID, low.blocking_items)
        high = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id, permission_id, session_id, boundary_id, ttl_seconds=61
            )
        )
        self.assertIn(BLOCK_INVOCATION_TTL_INVALID, high.blocking_items)
        with self.assertRaises(ProductionRuntimeInvocationError):
            reserve_production_runtime_invocation(
                **self._inv_kwargs(
                    activation_id,
                    permission_id,
                    session_id,
                    boundary_id,
                    ttl_seconds=9,
                )
            )

    # -- 10. TTL exceeds boundary ----------------------------------------------------

    def test_ttl_exceeds_boundary_blocked(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary(ttl_seconds=15)
        )
        summary = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id, permission_id, session_id, boundary_id, ttl_seconds=20
            )
        )
        self.assertIn(BLOCK_INVOCATION_TTL_EXCEEDS_BOUNDARY, summary.blocking_items)
        with self.assertRaises(ProductionRuntimeInvocationError):
            reserve_production_runtime_invocation(
                **self._inv_kwargs(
                    activation_id,
                    permission_id,
                    session_id,
                    boundary_id,
                    ttl_seconds=20,
                )
            )

    # -- 11. identity conflicts ------------------------------------------------------

    def test_identity_conflicts_blocked(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )

        mismatch = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                executor_id="other-runtime-executor",
            )
        )
        self.assertTrue(
            BLOCK_PERMISSION_EXECUTOR_MISMATCH in mismatch.blocking_items
            or BLOCK_SESSION_EXECUTOR_MISMATCH in mismatch.blocking_items
            or BLOCK_RUNTIME_BOUNDARY_EXECUTOR_MISMATCH in mismatch.blocking_items
        )

        same_as_executor = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                operator_id=_RUNTIME_EXECUTOR,
            )
        )
        self.assertTrue(
            BLOCK_IDENTITY_SEPARATION_INVALID in same_as_executor.blocking_items
            or BLOCK_OPERATOR_IDENTITY_INVALID in same_as_executor.blocking_items
        )

        same_as_boundary_operator = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                operator_id=_BOUNDARY_OPERATOR,
            )
        )
        self.assertIn(
            BLOCK_OPERATOR_IDENTITY_INVALID, same_as_boundary_operator.blocking_items
        )

        same_as_session_operator = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                operator_id=_SESSION_OPERATOR,
            )
        )
        self.assertIn(
            BLOCK_OPERATOR_IDENTITY_INVALID, same_as_session_operator.blocking_items
        )

        same_as_permission_operator = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                operator_id=_PERMISSION_OPERATOR,
            )
        )
        self.assertIn(
            BLOCK_OPERATOR_IDENTITY_INVALID,
            same_as_permission_operator.blocking_items,
        )

    # -- 12. recovery required --------------------------------------------------------

    def test_recovery_required_blocked(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
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
            summary = evaluate_production_runtime_invocation(
                **self._inv_kwargs(
                    activation_id,
                    permission_id,
                    session_id,
                    boundary_id,
                    operator_id="",
                    executor_id="",
                )
            )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, summary.blocking_items)

    # -- 13. force flags never leak into the summary -----------------------------------

    def test_force_flags_blocked_summary_stays_false(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        forced = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
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
        self.assertIn(BLOCK_RUNTIME_ALREADY_INVOKED, forced.blocking_items)
        self.assertFalse(forced.production_execution_allowed)
        self.assertFalse(forced.cutover_started)
        self.assertFalse(forced.runtime_invoked)
        self.assertFalse(forced.permission_consumed)
        self.assertFalse(forced.permission_revoked)
        self.assertFalse(forced.execution_phrase_verified)
        self.assertFalse(forced.boundary_runtime_invoked)
        self.assertFalse(forced.boundary_cutover_started)

    # -- 14. failsafe / factory / invoker availability -------------------------------------

    def test_force_kill_emergency_factory_invoker_blocked(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        kill = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                force_kill_switch_unavailable=True,
            )
        )
        self.assertIn(BLOCK_KILL_SWITCH_UNAVAILABLE, kill.blocking_items)
        emergency = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                force_emergency_close_unavailable=True,
            )
        )
        self.assertIn(BLOCK_EMERGENCY_CLOSE_UNAVAILABLE, emergency.blocking_items)
        factory = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                force_runtime_factory_unavailable=True,
            )
        )
        self.assertIn(BLOCK_RUNTIME_FACTORY_UNAVAILABLE, factory.blocking_items)
        invoker = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                force_runtime_invoker_enabled=True,
            )
        )
        self.assertIn(BLOCK_RUNTIME_INVOKER_ENABLED, invoker.blocking_items)

    # -- 15. idempotent duplicate reserve -----------------------------------------------

    def test_duplicate_same_reserve_idempotent(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        first = reserve_production_runtime_invocation(
            **self._inv_kwargs(activation_id, permission_id, session_id, boundary_id)
        )
        second = reserve_production_runtime_invocation(
            **self._inv_kwargs(activation_id, permission_id, session_id, boundary_id)
        )
        self.assertEqual(first.runtime_invocation_id, second.runtime_invocation_id)
        self.assertEqual(second.invocation_state, INVOCATION_RESERVED)
        paths = list(self.invocation_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)
        second_eval = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id, permission_id, session_id, boundary_id, operator_id=""
            )
        )
        self.assertIn(
            BLOCK_RUNTIME_INVOCATION_ALREADY_RESERVED, second_eval.blocking_items
        )

    # -- 16. changed executor / TTL conflicts --------------------------------------------

    def test_duplicate_changed_executor_and_ttl_conflict(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        reserve_production_runtime_invocation(
            **self._inv_kwargs(activation_id, permission_id, session_id, boundary_id)
        )
        with self.assertRaises(ProductionRuntimeInvocationError) as ctx:
            reserve_production_runtime_invocation(
                **self._inv_kwargs(
                    activation_id,
                    permission_id,
                    session_id,
                    boundary_id,
                    executor_id="other-runtime-executor",
                )
            )
        self.assertIn("runtime_invocation_conflict", str(ctx.exception))

        with self.assertRaises(ProductionRuntimeInvocationError) as ctx2:
            reserve_production_runtime_invocation(
                **self._inv_kwargs(
                    activation_id,
                    permission_id,
                    session_id,
                    boundary_id,
                    ttl_seconds=20,
                )
            )
        self.assertIn("runtime_invocation_conflict", str(ctx2.exception))

        conflict_eval = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                ttl_seconds=20,
            )
        )
        self.assertIn(
            BLOCK_GOVERNED_RUNTIME_INVOCATION_CONFLICT, conflict_eval.blocking_items
        )

    # -- 17. concurrent reserve -----------------------------------------------------------

    def test_concurrent_reserve_one_success(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        results: list[str] = []
        errors: list[str] = []

        def worker() -> None:
            try:
                summary = reserve_production_runtime_invocation(
                    **self._inv_kwargs(
                        activation_id, permission_id, session_id, boundary_id
                    )
                )
                results.append(summary.runtime_invocation_id)
            except ProductionRuntimeInvocationError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(results)
        self.assertEqual(len(set(results)), 1)
        paths = list(self.invocation_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 18. expiry and re-reserve --------------------------------------------------------

    def test_expired_invocation_derived_and_re_reserve_blocked(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        reserved = reserve_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                ttl_seconds=10,
            )
        )
        self.assertEqual(reserved.invocation_state, INVOCATION_RESERVED)
        later = self._now + timedelta(seconds=11)
        expired = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id,
                permission_id,
                session_id,
                boundary_id,
                now=later,
            )
        )
        self.assertEqual(expired.invocation_state, INVOCATION_EXPIRED)
        with self.assertRaises(ProductionRuntimeInvocationError) as ctx:
            reserve_production_runtime_invocation(
                **self._inv_kwargs(
                    activation_id,
                    permission_id,
                    session_id,
                    boundary_id,
                    now=later,
                )
            )
        self.assertIn("runtime_invocation_expired", str(ctx.exception))

    # -- 19. GovernedRuntimeInvocationContext ----------------------------------------------

    def test_context_valid_nested_reuse_and_pickle_blocked(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        reserved = reserve_production_runtime_invocation(
            **self._inv_kwargs(activation_id, permission_id, session_id, boundary_id)
        )
        context = GovernedRuntimeInvocationContext(
            **self._invocation_context_kwargs(activation_id)
        )
        with context as ctx:
            self.assertTrue(ctx.active)
            self.assertEqual(ctx.runtime_invocation_id, reserved.runtime_invocation_id)
            self.assertEqual(ctx.boundary_id, boundary_id)
            self.assertEqual(ctx.session_id, session_id)
            self.assertEqual(ctx.permission_id, permission_id)
            with self.assertRaises(ProductionRuntimeInvocationError):
                with context:
                    pass
        self.assertFalse(context.active)
        with self.assertRaises(ProductionRuntimeInvocationError):
            with context:
                pass
        with self.assertRaises(ProductionRuntimeInvocationError):
            pickle.dumps(context)

    # -- 20. safe output + CLI parsers -----------------------------------------------------

    def test_safe_output_and_cli_parsers(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        reserved = reserve_production_runtime_invocation(
            **self._inv_kwargs(activation_id, permission_id, session_id, boundary_id)
        )
        output = format_production_runtime_invocation_status(reserved)
        self.assertIn("invocation_state:", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("execution_phrase_required: true", output)
        self.assertIn("execution_phrase_verified: false", output)
        self.assertNotIn(_RUNTIME_EXECUTOR, output)
        self.assertNotIn(_INVOCATION_OPERATOR, output)
        self.assertNotIn(_BOUNDARY_OPERATOR, output)
        self.assertNotIn(_SESSION_OPERATOR, output)
        self.assertNotIn(_PERMISSION_OPERATOR, output)
        self.assertNotIn(str(self.hermes_home), output)
        self.assertNotIn("/opt/data/", output)
        parser = build_coo_dispatch_parser()
        for cmd, handler in (
            (
                [
                    "production", "governed-cutover", "runtime-invocation", "status",
                    "--activation-request-id", activation_id,
                ],
                "_cmd_production_runtime_invocation_status",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-invocation", "check",
                    "--activation-request-id", activation_id,
                    "--boundary-id", boundary_id,
                    "--session-id", session_id,
                    "--permission-id", permission_id,
                ],
                "_cmd_production_runtime_invocation_check",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-invocation", "reserve",
                    "--activation-request-id", activation_id,
                    "--boundary-id", boundary_id,
                    "--session-id", session_id,
                    "--permission-id", permission_id,
                    "--executor-id", _RUNTIME_EXECUTOR,
                    "--operator-id", _INVOCATION_OPERATOR,
                    "--ttl-seconds", "30",
                ],
                "_cmd_production_runtime_invocation_reserve",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-invocation", "show",
                    "--runtime-invocation-id", reserved.runtime_invocation_id,
                ],
                "_cmd_production_runtime_invocation_show",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-invocation", "history",
                    "--activation-request-id", activation_id,
                ],
                "_cmd_production_runtime_invocation_history",
            ),
        ):
            args = parser.parse_args(cmd)
            self.assertEqual(args.handler.__name__, handler)
        legacy = parser.parse_args(
            ["production", "cutover-check", "--ticket-id", "ticket-x"]
        )
        self.assertEqual(legacy.handler.__name__, "_cmd_production_cutover_check")
        boundary_status = parser.parse_args(
            [
                "production", "governed-cutover", "runtime-boundary", "status",
                "--activation-request-id", activation_id,
            ]
        )
        self.assertEqual(
            boundary_status.handler.__name__, "_cmd_production_runtime_boundary_status"
        )

    # -- 21. dashboard + release summary -----------------------------------------------------

    def test_dashboard_and_release_summary(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        reserved = reserve_production_runtime_invocation(
            **self._inv_kwargs(activation_id, permission_id, session_id, boundary_id)
        )
        digest = resolve_latest_governed_runtime_invocation_dashboard_digest(
            governed_cutover_store_dir=self.governed_cutover_store_dir,
            window_store_dir=self.window_store_dir,
            permission_store_dir=self.permission_store_dir,
            session_store_dir=self.session_store_dir,
            boundary_store_dir=self.boundary_store_dir,
            invocation_store_dir=self.invocation_store_dir,
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
        self.assertEqual(
            digest.governed_runtime_invocation_state, INVOCATION_RESERVED
        )
        self.assertTrue(digest.governed_runtime_invocation_present)
        self.assertEqual(
            digest.governed_runtime_invocation_id, reserved.runtime_invocation_id
        )
        self.assertFalse(digest.governed_runtime_invocation_phrase_verified)

        boundary_digest = _bound.resolve_latest_runtime_boundary_dashboard_digest(
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
        self.assertEqual(boundary_digest.runtime_boundary_state, BOUNDARY_RESERVED)
        self.assertEqual(boundary_digest.runtime_boundary_id, boundary_id)

        dashboard = build_operator_dashboard_summary(merged_config={})
        self.assertTrue(hasattr(dashboard, "governed_runtime_invocation_state"))
        self.assertTrue(hasattr(dashboard, "runtime_boundary_state"))

        release = build_production_runtime_invocation_release_summary(reserved)
        self.assertEqual(
            release.release_status, RELEASE_GOVERNED_RUNTIME_INVOCATION_RESERVED
        )
        self.assertEqual(release.next_phase, "Phase_15G_execution_authorization")
        self.assertFalse(release.production_execution_allowed)
        self.assertFalse(release.cutover_started)
        self.assertFalse(release.runtime_invoked)
        self.assertFalse(release.permission_consumed)
        self.assertFalse(release.permission_revoked)
        self.assertFalse(release.execution_phrase_verified)
        self.assertTrue(release.execution_phrase_required)

        contract = load_governed_cutover_contract(
            activation_id, store_dir=self.governed_cutover_store_dir
        )
        assert contract is not None
        self.assertFalse(contract.cutover_started)

    # -- 22. no subprocess on reserve --------------------------------------------------------

    def test_no_subprocess_on_reserve(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                reserve_production_runtime_invocation(
                    **self._inv_kwargs(
                        activation_id, permission_id, session_id, boundary_id
                    )
                )

    # -- 23. blocked when boundary already runtime-invoked / cutover-started ------------------

    def test_boundary_runtime_invoked_or_cutover_started_blocks_invocation(
        self,
    ) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        # `force_*` on the boundary's own evaluate never persists into the
        # boundary record (invariant enforced by Phase 15E); this asserts
        # that the invocation module reads the boundary record's *persisted*
        # (always-False) flags rather than trusting caller-provided force
        # hints, i.e. the boundary stays valid for invocation purposes.
        summary = evaluate_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id, permission_id, session_id, boundary_id, operator_id=""
            )
        )
        self.assertFalse(summary.boundary_runtime_invoked)
        self.assertFalse(summary.boundary_cutover_started)
        self.assertTrue(summary.boundary_valid)


if __name__ == "__main__":
    unittest.main()
