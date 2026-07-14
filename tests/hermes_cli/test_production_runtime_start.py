"""Phase 15H tests — governed runtime start contract."""

from __future__ import annotations

import json
import pickle
import subprocess
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

from agent.coo.dispatch_gateway_operator_dashboard import (
    build_operator_dashboard_summary,
)
from agent.coo.production_execution_authorization import (
    AUTHORIZATION_ISSUED,
)
from agent.coo.production_governed_cutover import load_governed_cutover_contract
from agent.coo.production_runtime_start import (
    ACTION_RUNTIME_START_STARTED_WAIT_FOR_PHASE_15I,
    ACTION_START_GOVERNED_RUNTIME,
    BLOCK_CONTROLLED_WINDOW_CLOSED,
    BLOCK_EXECUTION_AUTHORIZATION_CONSUMED,
    BLOCK_EXECUTION_AUTHORIZATION_EXPIRED,
    BLOCK_EXECUTION_AUTHORIZATION_MISSING,
    BLOCK_EXECUTION_AUTHORIZATION_REVOKED,
    BLOCK_IDENTITY_SEPARATION_INVALID,
    BLOCK_KILL_SWITCH_UNAVAILABLE,
    BLOCK_OPERATOR_IDENTITY_INVALID,
    BLOCK_PRODUCTION_EXECUTION_ENABLED,
    BLOCK_RUNTIME_ALREADY_INVOKED,
    BLOCK_RUNTIME_START_ALREADY_STARTED,
    BLOCK_RUNTIME_START_CONFLICT,
    BLOCK_RUNTIME_START_TTL_EXCEEDS_AUTHORIZATION,
    BLOCK_RUNTIME_START_TTL_INVALID,
    BLOCK_SUPERVISOR_IDENTITY_INVALID,
    EVENT_RUNTIME_EXECUTION_BLOCKED,
    EVENT_RUNTIME_START_REQUESTED,
    EVENT_RUNTIME_START_STARTED,
    RELEASE_GOVERNED_RUNTIME_START_STARTED,
    RUNTIME_START_BLOCKED,
    RUNTIME_START_EXPIRED,
    RUNTIME_START_READY,
    RUNTIME_START_STARTED,
    GovernedRuntimeStartContext,
    ProductionRuntimeStartError,
    build_production_runtime_start_release_summary,
    default_runtime_start_store_dir,
    evaluate_production_runtime_start,
    format_production_runtime_start_status,
    load_runtime_start_by_id,
    load_runtime_start_events,
    load_runtime_start_record,
    resolve_latest_governed_runtime_start_dashboard_digest,
    start_production_runtime_start,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli import test_production_execution_authorization as _auth
from tests.hermes_cli.test_production_controlled_window import _WINDOW_OPERATOR
from agent.coo.production_controlled_window import (
    REASON_OPERATOR_CLOSE,
    close_production_controlled_window,
)
from tests.hermes_cli.test_production_runtime_permission import _RUNTIME_EXECUTOR

_RUNTIME_START_OPERATOR = "runtime-start-operator-phase15h"
_RUNTIME_START_SUPERVISOR = "runtime-start-supervisor-phase15h"


class TestProductionRuntimeStart(_auth.TestProductionExecutionAuthorization):
    def setUp(self) -> None:
        super().setUp()
        self.runtime_start_store_dir = (
            self.hermes_home / "coo" / "production-runtime-start"
        )
        self.runtime_start_store_dir.mkdir(parents=True, exist_ok=True)

    # -- helpers ------------------------------------------------------------

    def _authorize_chain(
        self, **overrides
    ) -> tuple[str, str, str, str, str, str]:
        auth_ttl = overrides.pop("authorization_ttl_seconds", 20)
        activation_id, permission_id, session_id, boundary_id, runtime_invocation_id = (
            self._reserve_invocation()
        )
        authorized = self._authorize(
            activation_id,
            runtime_invocation_id,
            ttl_seconds=auth_ttl,
            **overrides,
        )
        return (
            activation_id,
            permission_id,
            session_id,
            boundary_id,
            runtime_invocation_id,
            authorized.authorization_id,
        )

    def _start_kwargs(self, activation_id: str, authorization_id: str, **overrides):
        base = {
            **self._eval_kwargs(activation_id),
            "runtime_start_store_dir": self.runtime_start_store_dir,
            "authorization_store_dir": self.authorization_store_dir,
            "invocation_store_dir": self.invocation_store_dir,
            "boundary_store_dir": self.boundary_store_dir,
            "session_store_dir": self.session_store_dir,
            "permission_store_dir": self.permission_store_dir,
            "authorization_id": authorization_id,
            "executor_id": _RUNTIME_EXECUTOR,
            "operator_id": _RUNTIME_START_OPERATOR,
            "supervisor_id": _RUNTIME_START_SUPERVISOR,
            "ttl_seconds": 15,
        }
        base.update(overrides)
        return base

    def _start(self, activation_id: str, authorization_id: str, **overrides):
        kwargs = self._start_kwargs(activation_id, authorization_id, **overrides)
        return start_production_runtime_start(**kwargs)

    def _start_context_kwargs(self, activation_id: str, **overrides):
        base = {
            "activation_request_id": activation_id,
            "runtime_start_store_dir": self.runtime_start_store_dir,
            "authorization_store_dir": self.authorization_store_dir,
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

    def _runtime_start_correlated_digests(self, activation_id: str):
        base = self._authorization_correlated_digests(activation_id)
        authorization_path = self.authorization_store_dir / f"{activation_id}.json"
        return (*base, self._digest(authorization_path))

    # -- 1. ready when authorization issued + phrase verified + full chain --

    def test_ready_when_authorization_issued(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        summary = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                operator_id="",
                supervisor_id="",
            )
        )
        self.assertEqual(summary.runtime_start_state, RUNTIME_START_READY)
        self.assertTrue(summary.runtime_start_ready)
        self.assertTrue(summary.execution_authorization_valid)
        self.assertTrue(summary.execution_authorization_phrase_verified)
        self.assertEqual(summary.recommended_action, ACTION_START_GOVERNED_RUNTIME)
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.permission_consumed)
        self.assertFalse(summary.authorization_consumed)
        self.assertFalse(summary.runtime_started)

    # -- 2. check is read-only; digests unchanged ----------------------------

    def test_check_read_only_digests_unchanged(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        before = self._runtime_start_correlated_digests(activation_id)
        evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id, authorization_id, operator_id="", supervisor_id=""
            )
        )
        after = self._runtime_start_correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertFalse(list(self.runtime_start_store_dir.glob("*.json")))

    # -- 3. start success -----------------------------------------------------

    def test_start_success(self) -> None:
        (
            activation_id,
            permission_id,
            session_id,
            boundary_id,
            runtime_invocation_id,
            authorization_id,
        ) = self._authorize_chain()
        before = self._runtime_start_correlated_digests(activation_id)
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                summary = self._start(activation_id, authorization_id)
        after = self._runtime_start_correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertEqual(summary.runtime_start_state, RUNTIME_START_STARTED)
        self.assertTrue(summary.runtime_start_id)
        self.assertEqual(summary.authorization_id, authorization_id)
        self.assertEqual(summary.runtime_invocation_id, runtime_invocation_id)
        self.assertEqual(summary.boundary_id, boundary_id)
        self.assertEqual(summary.session_id, session_id)
        self.assertEqual(summary.permission_id, permission_id)
        self.assertTrue(summary.runtime_started)
        events = load_runtime_start_events(
            activation_id, store_dir=self.runtime_start_store_dir
        )
        types = [event.event_type for event in events]
        self.assertIn(EVENT_RUNTIME_START_REQUESTED, types)
        self.assertIn(EVENT_RUNTIME_START_STARTED, types)
        self.assertIn(EVENT_RUNTIME_EXECUTION_BLOCKED, types)
        self.assertNotIn("cutover_started", types)
        self.assertNotIn("permission_consumed", types)
        self.assertNotIn("runtime_invoked", types)
        self.assertNotIn("authorization_consumed", types)

    # -- 4. all safety flags remain false except runtime_started ------------

    def test_start_all_flags_false_except_runtime_started(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        summary = self._start(activation_id, authorization_id)
        self.assertTrue(summary.runtime_started)
        self.assertFalse(summary.production_execution_allowed)
        self.assertTrue(summary.production_root_hard_deny)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.permission_consumed)
        self.assertFalse(summary.permission_revoked)
        self.assertFalse(summary.authorization_consumed)
        self.assertFalse(summary.original_repository2_execution_attempted)
        self.assertFalse(summary.gateway_production_enabled)
        self.assertFalse(summary.discord_production_enabled)
        self.assertFalse(summary.external_publish_enabled)
        self.assertFalse(summary.boundary_runtime_invoked)
        self.assertFalse(summary.boundary_cutover_started)
        self.assertFalse(summary.invocation_runtime_invoked)
        self.assertFalse(summary.invocation_cutover_started)

        record = load_runtime_start_record(
            activation_id, store_dir=self.runtime_start_store_dir
        )
        assert record is not None
        self.assertTrue(record.runtime_started)
        self.assertFalse(record.consumed)
        self.assertFalse(record.revoked)
        self.assertFalse(record.production_execution_allowed)
        self.assertTrue(record.production_root_hard_deny)
        self.assertFalse(record.cutover_started)
        self.assertFalse(record.runtime_invoked)
        self.assertFalse(record.permission_consumed)
        self.assertFalse(record.permission_revoked)
        self.assertFalse(record.authorization_consumed)

        # Upstream 15A-15G artifacts are never mutated by this phase.
        authorization_record = _auth.load_execution_authorization_record(
            activation_id, store_dir=self.authorization_store_dir
        )
        assert authorization_record is not None
        self.assertFalse(authorization_record.consumed)
        self.assertFalse(authorization_record.revoked)
        contract = load_governed_cutover_contract(
            activation_id, store_dir=self.governed_cutover_store_dir
        )
        assert contract is not None
        self.assertFalse(contract.cutover_started)

    # -- 5. missing / expired authorization blocked --------------------------

    def test_missing_and_expired_authorization_blocked(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        missing = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id, "", operator_id="", supervisor_id=""
            )
        )
        self.assertIn(BLOCK_EXECUTION_AUTHORIZATION_MISSING, missing.blocking_items)
        with self.assertRaises(ProductionRuntimeStartError):
            self._start(activation_id, "")

        (
            activation_id2,
            _p2,
            _s2,
            _b2,
            _inv2,
            authorization_id2,
        ) = self._authorize_chain(authorization_ttl_seconds=10)
        later = self._now + timedelta(seconds=11)
        expired = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id2,
                authorization_id2,
                operator_id="",
                supervisor_id="",
                now=later,
            )
        )
        self.assertIn(
            BLOCK_EXECUTION_AUTHORIZATION_EXPIRED, expired.blocking_items
        )
        with self.assertRaises(ProductionRuntimeStartError):
            self._start(activation_id2, authorization_id2, now=later)

    # -- 6. consumed / revoked authorization blocked --------------------------

    def test_consumed_and_revoked_authorization_blocked(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        consumed = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                force_authorization_consumed=True,
            )
        )
        self.assertIn(
            BLOCK_EXECUTION_AUTHORIZATION_CONSUMED, consumed.blocking_items
        )
        self.assertFalse(consumed.authorization_consumed)

        revoked = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                force_authorization_revoked=True,
            )
        )
        self.assertIn(BLOCK_EXECUTION_AUTHORIZATION_REVOKED, revoked.blocking_items)

    # -- 7. upstream window closed blocks start -------------------------------

    def test_upstream_window_closed_blocks_start(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                reason_code=REASON_OPERATOR_CLOSE,
            )
        )
        summary = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id, authorization_id, operator_id="", supervisor_id=""
            )
        )
        self.assertIn(BLOCK_CONTROLLED_WINDOW_CLOSED, summary.blocking_items)
        with self.assertRaises(ProductionRuntimeStartError):
            self._start(activation_id, authorization_id)

    # -- 8. TTL bounds and exceeds-authorization blocked ----------------------

    def test_ttl_bounds_and_exceeds_authorization_blocked(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        low = evaluate_production_runtime_start(
            **self._start_kwargs(activation_id, authorization_id, ttl_seconds=4)
        )
        self.assertIn(BLOCK_RUNTIME_START_TTL_INVALID, low.blocking_items)
        high = evaluate_production_runtime_start(
            **self._start_kwargs(activation_id, authorization_id, ttl_seconds=31)
        )
        self.assertIn(BLOCK_RUNTIME_START_TTL_INVALID, high.blocking_items)
        with self.assertRaises(ProductionRuntimeStartError):
            self._start(activation_id, authorization_id, ttl_seconds=4)

        (
            activation_id2,
            _p2,
            _s2,
            _b2,
            _inv2,
            authorization_id2,
        ) = self._authorize_chain(authorization_ttl_seconds=10)
        exceeds = evaluate_production_runtime_start(
            **self._start_kwargs(activation_id2, authorization_id2, ttl_seconds=15)
        )
        self.assertIn(
            BLOCK_RUNTIME_START_TTL_EXCEEDS_AUTHORIZATION, exceeds.blocking_items
        )
        with self.assertRaises(ProductionRuntimeStartError):
            self._start(activation_id2, authorization_id2, ttl_seconds=15)

    # -- 9. identity conflicts (3-way: executor/operator/supervisor) --------

    def test_identity_conflicts_blocked(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()

        operator_same_as_executor = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                operator_id=_RUNTIME_EXECUTOR,
            )
        )
        self.assertTrue(
            BLOCK_IDENTITY_SEPARATION_INVALID
            in operator_same_as_executor.blocking_items
            or BLOCK_OPERATOR_IDENTITY_INVALID
            in operator_same_as_executor.blocking_items
        )

        supervisor_same_as_operator = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                supervisor_id=_RUNTIME_START_OPERATOR,
            )
        )
        self.assertTrue(
            BLOCK_IDENTITY_SEPARATION_INVALID
            in supervisor_same_as_operator.blocking_items
            or BLOCK_SUPERVISOR_IDENTITY_INVALID
            in supervisor_same_as_operator.blocking_items
        )

        supervisor_same_as_authorization_signer = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                supervisor_id=_auth._AUTHORIZATION_SIGNER,
            )
        )
        self.assertIn(
            BLOCK_SUPERVISOR_IDENTITY_INVALID,
            supervisor_same_as_authorization_signer.blocking_items,
        )

        operator_same_as_authorization_operator = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                operator_id=_auth._AUTHORIZATION_OPERATOR,
            )
        )
        self.assertIn(
            BLOCK_OPERATOR_IDENTITY_INVALID,
            operator_same_as_authorization_operator.blocking_items,
        )

        with self.assertRaises(ProductionRuntimeStartError):
            self._start(
                activation_id,
                authorization_id,
                supervisor_id=_auth._AUTHORIZATION_SIGNER,
            )

    # -- 10. force flags never leak into the summary --------------------------

    def test_force_flags_blocked_summary_stays_false(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        forced = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                force_production_execution_allowed=True,
                force_gateway_enabled=True,
                force_discord_enabled=True,
                force_cutover_started=True,
                force_runtime_invoked=True,
                force_permission_consumed=True,
                force_permission_revoked=True,
                force_authorization_consumed=True,
                force_authorization_revoked=True,
            )
        )
        self.assertIn(BLOCK_PRODUCTION_EXECUTION_ENABLED, forced.blocking_items)
        self.assertIn(BLOCK_RUNTIME_ALREADY_INVOKED, forced.blocking_items)
        self.assertFalse(forced.production_execution_allowed)
        self.assertFalse(forced.cutover_started)
        self.assertFalse(forced.runtime_invoked)
        self.assertFalse(forced.permission_consumed)
        self.assertFalse(forced.permission_revoked)
        self.assertFalse(forced.authorization_consumed)
        self.assertFalse(forced.runtime_started)

    # -- 11. failsafe availability ---------------------------------------------

    def test_force_kill_switch_unavailable_blocked(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        summary = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                force_kill_switch_unavailable=True,
            )
        )
        self.assertIn(BLOCK_KILL_SWITCH_UNAVAILABLE, summary.blocking_items)

    # -- 12. idempotent duplicate start ----------------------------------------

    def test_duplicate_same_start_idempotent(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        first = self._start(activation_id, authorization_id)
        second = self._start(activation_id, authorization_id)
        self.assertEqual(first.runtime_start_id, second.runtime_start_id)
        self.assertEqual(second.runtime_start_state, RUNTIME_START_STARTED)
        self.assertTrue(second.already_started)
        paths = list(self.runtime_start_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

        check = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id, authorization_id, operator_id="", supervisor_id=""
            )
        )
        self.assertEqual(check.runtime_start_state, RUNTIME_START_STARTED)
        self.assertEqual(
            check.recommended_action, ACTION_RUNTIME_START_STARTED_WAIT_FOR_PHASE_15I
        )

    # -- 13. conflict on changed supervisor -------------------------------------

    def test_duplicate_changed_supervisor_conflict(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        self._start(activation_id, authorization_id)
        with self.assertRaises(ProductionRuntimeStartError) as ctx:
            self._start(
                activation_id,
                authorization_id,
                supervisor_id="different-runtime-start-supervisor",
            )
        self.assertIn("runtime_start_conflict", str(ctx.exception))

        conflict_eval = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                supervisor_id="different-runtime-start-supervisor",
            )
        )
        self.assertIn(BLOCK_RUNTIME_START_CONFLICT, conflict_eval.blocking_items)
        paths = list(self.runtime_start_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 14. concurrent start ----------------------------------------------------

    def test_concurrent_start_one_success(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        results: list[str] = []
        errors: list[str] = []

        def worker() -> None:
            try:
                summary = self._start(activation_id, authorization_id)
                results.append(summary.runtime_start_id)
            except ProductionRuntimeStartError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(results)
        self.assertEqual(len(set(results)), 1)
        paths = list(self.runtime_start_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 15. expired derived state; no restart ----------------------------------

    def test_expired_runtime_start_derived_and_restart_blocked(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        started = self._start(activation_id, authorization_id, ttl_seconds=10)
        self.assertEqual(started.runtime_start_state, RUNTIME_START_STARTED)
        later = self._now + timedelta(seconds=11)
        expired = evaluate_production_runtime_start(
            **self._start_kwargs(
                activation_id,
                authorization_id,
                operator_id="",
                supervisor_id="",
                now=later,
            )
        )
        self.assertEqual(expired.runtime_start_state, RUNTIME_START_EXPIRED)
        with self.assertRaises(ProductionRuntimeStartError) as ctx:
            self._start(
                activation_id, authorization_id, ttl_seconds=10, now=later
            )
        self.assertIn("runtime_start_expired", str(ctx.exception))
        paths = list(self.runtime_start_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 16. GovernedRuntimeStartContext -----------------------------------------

    def test_context_valid_nested_reuse_and_pickle_blocked(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        started = self._start(activation_id, authorization_id)
        context = GovernedRuntimeStartContext(
            **self._start_context_kwargs(activation_id)
        )
        with context as ctx:
            self.assertTrue(ctx.active)
            self.assertEqual(ctx.runtime_start_id, started.runtime_start_id)
            self.assertEqual(ctx.authorization_id, authorization_id)
            self.assertTrue(ctx.runtime_started)
            with self.assertRaises(ProductionRuntimeStartError):
                with context:
                    pass
        self.assertFalse(context.active)
        with self.assertRaises(ProductionRuntimeStartError):
            with context:
                pass
        with self.assertRaises(ProductionRuntimeStartError):
            pickle.dumps(context)

    def test_context_expired_rejected(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        self._start(activation_id, authorization_id, ttl_seconds=10)
        later = self._now + timedelta(seconds=11)
        context = GovernedRuntimeStartContext(
            **self._start_context_kwargs(activation_id, now=later)
        )
        with self.assertRaises(ProductionRuntimeStartError):
            with context:
                pass

    def test_context_not_started_rejected(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        context = GovernedRuntimeStartContext(
            **self._start_context_kwargs(activation_id)
        )
        with self.assertRaises(ProductionRuntimeStartError):
            with context:
                pass

    # -- 17. safe output + CLI parsers (+ legacy unchanged) ----------------------

    def test_safe_output_and_cli_parsers(self) -> None:
        (
            activation_id,
            _permission_id,
            _session_id,
            _boundary_id,
            runtime_invocation_id,
            authorization_id,
        ) = self._authorize_chain()
        started = self._start(activation_id, authorization_id)
        output = format_production_runtime_start_status(started)
        self.assertIn("runtime_start_state:", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("runtime_started: true", output)
        self.assertIn("runtime_invoked: false", output)
        self.assertIn("cutover_started: false", output)
        self.assertIn("authorization_consumed: false", output)
        self.assertNotIn(_RUNTIME_EXECUTOR, output)
        self.assertNotIn(_RUNTIME_START_OPERATOR, output)
        self.assertNotIn(_RUNTIME_START_SUPERVISOR, output)
        self.assertNotIn(str(self.hermes_home), output)
        self.assertNotIn("/opt/data/", output)
        self.assertNotIn("CONFIRM-REPOSITORY2-EXECUTION", output)

        parser = build_coo_dispatch_parser()
        for cmd, handler in (
            (
                [
                    "production", "governed-cutover", "runtime-start",
                    "status", "--activation-request-id", activation_id,
                ],
                "_cmd_production_runtime_start_status",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-start",
                    "check",
                    "--activation-request-id", activation_id,
                    "--authorization-id", authorization_id,
                ],
                "_cmd_production_runtime_start_check",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-start",
                    "start",
                    "--activation-request-id", activation_id,
                    "--authorization-id", authorization_id,
                    "--executor-id", _RUNTIME_EXECUTOR,
                    "--operator-id", _RUNTIME_START_OPERATOR,
                    "--supervisor-id", _RUNTIME_START_SUPERVISOR,
                    "--ttl-seconds", "15",
                ],
                "_cmd_production_runtime_start_start",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-start",
                    "show", "--runtime-start-id", started.runtime_start_id,
                ],
                "_cmd_production_runtime_start_show",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-start",
                    "history", "--activation-request-id", activation_id,
                ],
                "_cmd_production_runtime_start_history",
            ),
        ):
            args = parser.parse_args(cmd)
            self.assertEqual(args.handler.__name__, handler)

        legacy = parser.parse_args(
            ["production", "cutover-check", "--ticket-id", "ticket-x"]
        )
        self.assertEqual(legacy.handler.__name__, "_cmd_production_cutover_check")
        authorization_status = parser.parse_args(
            [
                "production", "governed-cutover", "execution-authorization",
                "status", "--activation-request-id", activation_id,
            ]
        )
        self.assertEqual(
            authorization_status.handler.__name__,
            "_cmd_production_execution_authorization_status",
        )

    # -- 18. dashboard + release summary ------------------------------------------

    def test_dashboard_and_release_summary(self) -> None:
        (
            activation_id,
            permission_id,
            session_id,
            boundary_id,
            runtime_invocation_id,
            authorization_id,
        ) = self._authorize_chain()
        started = self._start(activation_id, authorization_id)

        digest = resolve_latest_governed_runtime_start_dashboard_digest(
            governed_cutover_store_dir=self.governed_cutover_store_dir,
            window_store_dir=self.window_store_dir,
            permission_store_dir=self.permission_store_dir,
            session_store_dir=self.session_store_dir,
            boundary_store_dir=self.boundary_store_dir,
            invocation_store_dir=self.invocation_store_dir,
            authorization_store_dir=self.authorization_store_dir,
            runtime_start_store_dir=self.runtime_start_store_dir,
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
        self.assertEqual(digest.governed_runtime_start_state, RUNTIME_START_STARTED)
        self.assertTrue(digest.governed_runtime_start_present)
        self.assertEqual(digest.governed_runtime_start_id, started.runtime_start_id)
        self.assertTrue(digest.governed_runtime_start_started)

        dashboard = build_operator_dashboard_summary(merged_config={})
        self.assertTrue(hasattr(dashboard, "governed_runtime_start_state"))
        self.assertTrue(hasattr(dashboard, "governed_runtime_start_started"))
        self.assertTrue(hasattr(dashboard, "execution_authorization_state"))

        release = build_production_runtime_start_release_summary(started)
        self.assertEqual(
            release.release_status, RELEASE_GOVERNED_RUNTIME_START_STARTED
        )
        self.assertEqual(release.next_phase, "Phase_15I_governed_runtime_invoke")
        self.assertFalse(release.production_execution_allowed)
        self.assertFalse(release.cutover_started)
        self.assertFalse(release.runtime_invoked)
        self.assertFalse(release.permission_consumed)
        self.assertFalse(release.permission_revoked)
        self.assertFalse(release.authorization_consumed)
        self.assertTrue(release.runtime_started)

        contract = load_governed_cutover_contract(
            activation_id, store_dir=self.governed_cutover_store_dir
        )
        assert contract is not None
        self.assertFalse(contract.cutover_started)

        by_id = load_runtime_start_by_id(
            started.runtime_start_id, store_dir=self.runtime_start_store_dir
        )
        assert by_id is not None
        self.assertEqual(by_id.runtime_start_id, started.runtime_start_id)

    # -- 19. no subprocess on start -----------------------------------------------

    def test_no_subprocess_on_start(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                self._start(activation_id, authorization_id)

    # -- 20. default store dir path -------------------------------------------------

    def test_default_store_dir_is_hermes_home_scoped(self) -> None:
        default_dir = default_runtime_start_store_dir()
        self.assertTrue(
            str(default_dir).endswith("coo/production-runtime-start")
        )

    # -- 21. runtime-start record never appears in authorization artifact ---------

    def test_runtime_start_artifact_not_mutated_by_authorization(self) -> None:
        (
            activation_id,
            _p,
            _s,
            _b,
            _inv_id,
            authorization_id,
        ) = self._authorize_chain()
        self._start(activation_id, authorization_id)
        path = self.runtime_start_store_dir / f"{activation_id}.json"
        raw_text = path.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
        self.assertTrue(payload["runtime_start"]["runtime_started"])
        self.assertFalse(payload["runtime_start"]["runtime_invoked"])
        self.assertFalse(payload["runtime_start"]["cutover_started"])
        self.assertFalse(payload["runtime_start"]["permission_consumed"])
        self.assertFalse(payload["runtime_start"]["authorization_consumed"])

    # -- 22. blocked state when core chain not ready ------------------------------

    def test_blocked_state_without_chain(self) -> None:
        summary = evaluate_production_runtime_start(
            activation_request_id="act-no-chain",
        )
        self.assertEqual(summary.runtime_start_state, RUNTIME_START_BLOCKED)
        self.assertFalse(summary.runtime_start_ready)
        self.assertFalse(summary.runtime_started)


if __name__ == "__main__":
    unittest.main()
