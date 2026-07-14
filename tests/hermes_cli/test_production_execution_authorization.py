"""Phase 15G tests — execution authorization contract."""

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
from agent.coo.production_executor_confirmation import REQUIRED_CONFIRMATION_PHRASE
from agent.coo.production_governed_cutover import load_governed_cutover_contract
from agent.coo.production_runtime_boundary import BOUNDARY_RESERVED
from agent.coo.production_runtime_invocation import (
    INVOCATION_RESERVED,
    load_runtime_invocation_record,
    reserve_production_runtime_invocation,
)
from agent.coo.production_execution_authorization import (
    ACTION_AUTHORIZE_GOVERNED_RUNTIME_EXECUTION,
    ACTION_EXECUTION_AUTHORIZED_WAIT_FOR_PHASE_15H,
    AUTHORIZATION_BLOCKED,
    AUTHORIZATION_EXPIRED,
    AUTHORIZATION_ISSUED,
    AUTHORIZATION_READY,
    BLOCK_AUTHORIZATION_TTL_EXCEEDS_INVOCATION,
    BLOCK_AUTHORIZATION_TTL_INVALID,
    BLOCK_CONTROLLED_WINDOW_CLOSED,
    BLOCK_EXECUTION_AUTHORIZATION_CONFLICT,
    BLOCK_IDENTITY_SEPARATION_INVALID,
    BLOCK_KILL_SWITCH_UNAVAILABLE,
    BLOCK_OPERATOR_IDENTITY_INVALID,
    BLOCK_PRODUCTION_EXECUTION_ENABLED,
    BLOCK_RUNTIME_ALREADY_INVOKED,
    BLOCK_RUNTIME_INVOCATION_EXPIRED,
    BLOCK_RUNTIME_INVOCATION_MISSING,
    BLOCK_SIGNER_IDENTITY_INVALID,
    EVENT_EXECUTION_AUTHORIZATION_ISSUED,
    EVENT_EXECUTION_AUTHORIZATION_REQUESTED,
    EVENT_EXECUTION_PHRASE_VERIFIED,
    EVENT_RUNTIME_EXECUTION_BLOCKED,
    RELEASE_EXECUTION_AUTHORIZATION_ISSUED,
    ExecutionAuthorizationContext,
    ProductionExecutionAuthorizationError,
    authorize_production_execution_authorization,
    build_production_execution_authorization_release_summary,
    default_execution_authorization_store_dir,
    evaluate_production_execution_authorization,
    format_production_execution_authorization_status,
    load_execution_authorization_by_id,
    load_execution_authorization_events,
    load_execution_authorization_record,
    resolve_latest_execution_authorization_dashboard_digest,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli import test_production_runtime_invocation as _inv
from tests.hermes_cli.test_production_controlled_window import _WINDOW_OPERATOR
from agent.coo.production_controlled_window import (
    REASON_OPERATOR_CLOSE,
    close_production_controlled_window,
)
from tests.hermes_cli.test_production_runtime_permission import _RUNTIME_EXECUTOR

_AUTHORIZATION_OPERATOR = "authorization-operator-phase15g"
_AUTHORIZATION_SIGNER = "authorization-signer-phase15g"


class TestProductionExecutionAuthorization(_inv.TestProductionRuntimeInvocation):
    def setUp(self) -> None:
        super().setUp()
        self.authorization_store_dir = (
            self.hermes_home / "coo" / "production-execution-authorization"
        )
        self.authorization_store_dir.mkdir(parents=True, exist_ok=True)

    # -- helpers ------------------------------------------------------------

    def _reserve_invocation(
        self, **overrides
    ) -> tuple[str, str, str, str, str]:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        invocation = reserve_production_runtime_invocation(
            **self._inv_kwargs(
                activation_id, permission_id, session_id, boundary_id, **overrides
            )
        )
        return (
            activation_id,
            permission_id,
            session_id,
            boundary_id,
            invocation.runtime_invocation_id,
        )

    def _auth_kwargs(
        self,
        activation_id: str,
        runtime_invocation_id: str,
        **overrides,
    ):
        base = {
            **self._eval_kwargs(activation_id),
            "authorization_store_dir": self.authorization_store_dir,
            "invocation_store_dir": self.invocation_store_dir,
            "boundary_store_dir": self.boundary_store_dir,
            "session_store_dir": self.session_store_dir,
            "permission_store_dir": self.permission_store_dir,
            "runtime_invocation_id": runtime_invocation_id,
            "executor_id": _RUNTIME_EXECUTOR,
            "operator_id": _AUTHORIZATION_OPERATOR,
            "signer_id": _AUTHORIZATION_SIGNER,
            "ttl_seconds": 15,
        }
        base.update(overrides)
        return base

    def _authorize(self, activation_id, runtime_invocation_id, **overrides):
        phrase = overrides.pop("phrase", REQUIRED_CONFIRMATION_PHRASE)
        kwargs = self._auth_kwargs(activation_id, runtime_invocation_id, **overrides)
        return authorize_production_execution_authorization(phrase=phrase, **kwargs)

    def _authorization_context_kwargs(self, activation_id: str, **overrides):
        base = {
            "activation_request_id": activation_id,
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

    def _authorization_correlated_digests(self, activation_id: str):
        base = self._invocation_correlated_digests(activation_id)
        invocation_path = self.invocation_store_dir / f"{activation_id}.json"
        return (*base, self._digest(invocation_path))

    # -- 1. check ready without phrase -------------------------------------

    def test_check_ready_without_phrase(self) -> None:
        activation_id, _permission_id, _session_id, _boundary_id, runtime_invocation_id = (
            self._reserve_invocation()
        )
        summary = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id,
                runtime_invocation_id,
                operator_id="",
                signer_id="",
                ttl_seconds=15,
            )
        )
        self.assertEqual(summary.authorization_state, AUTHORIZATION_READY)
        self.assertTrue(summary.authorization_ready)
        self.assertTrue(summary.invocation_valid)
        self.assertEqual(
            summary.recommended_action, ACTION_AUTHORIZE_GOVERNED_RUNTIME_EXECUTION
        )
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.permission_consumed)
        self.assertFalse(summary.permission_revoked)
        self.assertTrue(summary.execution_phrase_required)
        self.assertFalse(summary.execution_phrase_verified)

    # -- 2. read-only digests unchanged (invocation digest included) --------

    def test_check_read_only_digests_unchanged(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        before = self._authorization_correlated_digests(activation_id)
        evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id,
                runtime_invocation_id,
                operator_id="",
                signer_id="",
            )
        )
        after = self._authorization_correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertFalse(list(self.authorization_store_dir.glob("*.json")))

    # -- 3. authorize success ------------------------------------------------

    def test_authorize_success(self) -> None:
        activation_id, permission_id, session_id, boundary_id, runtime_invocation_id = (
            self._reserve_invocation()
        )
        before = self._authorization_correlated_digests(activation_id)
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                summary = self._authorize(activation_id, runtime_invocation_id)
        after = self._authorization_correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertEqual(summary.authorization_state, AUTHORIZATION_ISSUED)
        self.assertTrue(summary.authorization_id)
        self.assertEqual(summary.runtime_invocation_id, runtime_invocation_id)
        self.assertEqual(summary.boundary_id, boundary_id)
        self.assertEqual(summary.session_id, session_id)
        self.assertEqual(summary.permission_id, permission_id)
        self.assertTrue(summary.execution_phrase_verified)
        self.assertTrue(summary.execution_phrase_required)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.permission_consumed)
        events = load_execution_authorization_events(
            activation_id, store_dir=self.authorization_store_dir
        )
        types = [event.event_type for event in events]
        self.assertIn(EVENT_EXECUTION_AUTHORIZATION_REQUESTED, types)
        self.assertIn(EVENT_EXECUTION_PHRASE_VERIFIED, types)
        self.assertIn(EVENT_EXECUTION_AUTHORIZATION_ISSUED, types)
        self.assertIn(EVENT_RUNTIME_EXECUTION_BLOCKED, types)
        self.assertNotIn("cutover_started", types)
        self.assertNotIn("permission_consumed", types)
        self.assertNotIn("runtime_invoked", types)
        # The underlying invocation artifact is never mutated by this phase.
        invocation_record = load_runtime_invocation_record(
            activation_id, store_dir=self.invocation_store_dir
        )
        assert invocation_record is not None
        self.assertFalse(invocation_record.execution_phrase_verified)

    # -- 4. wrong phrase -> zero mutation ------------------------------------

    def test_wrong_phrase_zero_mutation(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        before = self._authorization_correlated_digests(activation_id)
        with self.assertRaises(ProductionExecutionAuthorizationError) as ctx:
            self._authorize(
                activation_id, runtime_invocation_id, phrase="WRONG-PHRASE"
            )
        self.assertIn("execution_phrase_invalid", str(ctx.exception))
        self.assertNotIn("WRONG-PHRASE", str(ctx.exception))
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE, str(ctx.exception))
        after = self._authorization_correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertFalse(list(self.authorization_store_dir.glob("*.json")))

    # -- 5. whitespace / case mismatch rejected ------------------------------

    def test_phrase_whitespace_and_case_mismatch_rejected(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        for bad_phrase in (
            f" {REQUIRED_CONFIRMATION_PHRASE}",
            f"{REQUIRED_CONFIRMATION_PHRASE} ",
            f" {REQUIRED_CONFIRMATION_PHRASE} ",
            REQUIRED_CONFIRMATION_PHRASE.lower(),
            REQUIRED_CONFIRMATION_PHRASE.replace("CONFIRM", "confirm"),
        ):
            with self.assertRaises(ProductionExecutionAuthorizationError) as ctx:
                self._authorize(
                    activation_id, runtime_invocation_id, phrase=bad_phrase
                )
            self.assertIn("execution_phrase_invalid", str(ctx.exception))
        self.assertFalse(list(self.authorization_store_dir.glob("*.json")))

    # -- 6. phrase never appears in artifact JSON or stdout ------------------

    def test_phrase_never_in_artifact_or_stdout(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        summary = self._authorize(activation_id, runtime_invocation_id)
        path = self.authorization_store_dir / f"{activation_id}.json"
        raw_text = path.read_text(encoding="utf-8")
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE, raw_text)
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE.lower(), raw_text.lower())
        payload = json.loads(raw_text)
        self.assertNotIn("phrase", json.dumps(payload).lower().replace(
            "execution_phrase_required", ""
        ).replace("execution_phrase_verified", ""))
        output = format_production_execution_authorization_status(summary)
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE, output)
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE.lower(), output.lower())

    # -- 7. authorize success flags (immutable safety) -----------------------

    def test_authorize_all_flags_false_except_phrase_verified(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        summary = self._authorize(activation_id, runtime_invocation_id)
        self.assertFalse(summary.production_execution_allowed)
        self.assertTrue(summary.production_root_hard_deny)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.permission_consumed)
        self.assertFalse(summary.permission_revoked)
        self.assertFalse(summary.original_repository2_execution_attempted)
        self.assertFalse(summary.gateway_production_enabled)
        self.assertFalse(summary.discord_production_enabled)
        self.assertFalse(summary.external_publish_enabled)
        self.assertFalse(summary.boundary_runtime_invoked)
        self.assertFalse(summary.boundary_cutover_started)
        self.assertFalse(summary.invocation_runtime_invoked)
        self.assertFalse(summary.invocation_cutover_started)
        self.assertTrue(summary.execution_phrase_verified)

        record = load_execution_authorization_record(
            activation_id, store_dir=self.authorization_store_dir
        )
        assert record is not None
        self.assertFalse(record.consumed)
        self.assertFalse(record.revoked)
        self.assertFalse(record.production_execution_allowed)
        self.assertTrue(record.production_root_hard_deny)
        self.assertFalse(record.cutover_started)
        self.assertFalse(record.runtime_invoked)
        self.assertFalse(record.permission_consumed)
        self.assertFalse(record.permission_revoked)
        self.assertTrue(record.execution_phrase_verified)

    # -- 8. missing / expired invocation blocked -----------------------------

    def test_missing_and_expired_invocation_blocked(self) -> None:
        activation_id, permission_id, session_id, boundary_id = (
            self._prepare_boundary()
        )
        missing = evaluate_production_execution_authorization(
            **self._auth_kwargs(activation_id, "", operator_id="", signer_id="")
        )
        self.assertIn(BLOCK_RUNTIME_INVOCATION_MISSING, missing.blocking_items)
        with self.assertRaises(ProductionExecutionAuthorizationError):
            self._authorize(activation_id, "")

        (
            activation_id2,
            _p2,
            _s2,
            _b2,
            runtime_invocation_id2,
        ) = self._reserve_invocation(ttl_seconds=10)
        later = self._now + timedelta(seconds=11)
        expired = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id2,
                runtime_invocation_id2,
                operator_id="",
                signer_id="",
                now=later,
            )
        )
        self.assertIn(BLOCK_RUNTIME_INVOCATION_EXPIRED, expired.blocking_items)
        with self.assertRaises(ProductionExecutionAuthorizationError):
            self._authorize(activation_id2, runtime_invocation_id2, now=later)

    # -- 9. upstream window invalid blocks authorization ---------------------

    def test_upstream_window_closed_blocks_authorization(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                reason_code=REASON_OPERATOR_CLOSE,
            )
        )
        summary = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id, runtime_invocation_id, operator_id="", signer_id=""
            )
        )
        self.assertIn(BLOCK_CONTROLLED_WINDOW_CLOSED, summary.blocking_items)
        with self.assertRaises(ProductionExecutionAuthorizationError):
            self._authorize(activation_id, runtime_invocation_id)

    # -- 10. TTL bounds and exceeds-invocation blocked -----------------------

    def test_ttl_bounds_and_exceeds_invocation_blocked(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        low = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id, runtime_invocation_id, ttl_seconds=4
            )
        )
        self.assertIn(BLOCK_AUTHORIZATION_TTL_INVALID, low.blocking_items)
        high = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id, runtime_invocation_id, ttl_seconds=31
            )
        )
        self.assertIn(BLOCK_AUTHORIZATION_TTL_INVALID, high.blocking_items)
        with self.assertRaises(ProductionExecutionAuthorizationError):
            self._authorize(activation_id, runtime_invocation_id, ttl_seconds=4)

        (
            activation_id2,
            _p2,
            _s2,
            _b2,
            runtime_invocation_id2,
        ) = self._reserve_invocation(ttl_seconds=10)
        exceeds = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id2, runtime_invocation_id2, ttl_seconds=15
            )
        )
        self.assertIn(
            BLOCK_AUTHORIZATION_TTL_EXCEEDS_INVOCATION, exceeds.blocking_items
        )
        with self.assertRaises(ProductionExecutionAuthorizationError):
            self._authorize(activation_id2, runtime_invocation_id2, ttl_seconds=15)

    # -- 11. identity conflicts (3-way) ---------------------------------------

    def test_identity_conflicts_blocked(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()

        operator_same_as_executor = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id,
                runtime_invocation_id,
                operator_id=_RUNTIME_EXECUTOR,
            )
        )
        self.assertTrue(
            BLOCK_IDENTITY_SEPARATION_INVALID
            in operator_same_as_executor.blocking_items
            or BLOCK_OPERATOR_IDENTITY_INVALID
            in operator_same_as_executor.blocking_items
        )

        signer_same_as_operator = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id,
                runtime_invocation_id,
                signer_id=_AUTHORIZATION_OPERATOR,
            )
        )
        self.assertTrue(
            BLOCK_IDENTITY_SEPARATION_INVALID
            in signer_same_as_operator.blocking_items
            or BLOCK_SIGNER_IDENTITY_INVALID in signer_same_as_operator.blocking_items
        )

        signer_same_as_invocation_operator = (
            evaluate_production_execution_authorization(
                **self._auth_kwargs(
                    activation_id,
                    runtime_invocation_id,
                    signer_id=_inv._INVOCATION_OPERATOR,
                )
            )
        )
        self.assertIn(
            BLOCK_SIGNER_IDENTITY_INVALID,
            signer_same_as_invocation_operator.blocking_items,
        )

        operator_same_as_invocation_operator = (
            evaluate_production_execution_authorization(
                **self._auth_kwargs(
                    activation_id,
                    runtime_invocation_id,
                    operator_id=_inv._INVOCATION_OPERATOR,
                )
            )
        )
        self.assertIn(
            BLOCK_OPERATOR_IDENTITY_INVALID,
            operator_same_as_invocation_operator.blocking_items,
        )

        with self.assertRaises(ProductionExecutionAuthorizationError):
            self._authorize(
                activation_id,
                runtime_invocation_id,
                signer_id=_inv._INVOCATION_OPERATOR,
            )

    # -- 12. force flags never leak into the summary --------------------------

    def test_force_flags_blocked_summary_stays_false(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        forced = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id,
                runtime_invocation_id,
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

    # -- 13. failsafe availability --------------------------------------------

    def test_force_kill_switch_unavailable_blocked(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        summary = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id,
                runtime_invocation_id,
                force_kill_switch_unavailable=True,
            )
        )
        self.assertIn(BLOCK_KILL_SWITCH_UNAVAILABLE, summary.blocking_items)

    # -- 14. idempotent duplicate authorize ------------------------------------

    def test_duplicate_same_authorize_idempotent(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        first = self._authorize(activation_id, runtime_invocation_id)
        second = self._authorize(activation_id, runtime_invocation_id)
        self.assertEqual(first.authorization_id, second.authorization_id)
        self.assertEqual(second.authorization_state, AUTHORIZATION_ISSUED)
        self.assertTrue(second.already_authorized)
        paths = list(self.authorization_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

        check = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id, runtime_invocation_id, operator_id="", signer_id=""
            )
        )
        self.assertEqual(check.authorization_state, AUTHORIZATION_ISSUED)
        self.assertEqual(
            check.recommended_action, ACTION_EXECUTION_AUTHORIZED_WAIT_FOR_PHASE_15H
        )

    # -- 15. conflict on signer change ------------------------------------------

    def test_duplicate_changed_signer_conflict(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        self._authorize(activation_id, runtime_invocation_id)
        with self.assertRaises(ProductionExecutionAuthorizationError) as ctx:
            self._authorize(
                activation_id,
                runtime_invocation_id,
                signer_id="different-authorization-signer",
            )
        self.assertIn("execution_authorization_conflict", str(ctx.exception))

        conflict_eval = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id,
                runtime_invocation_id,
                signer_id="different-authorization-signer",
            )
        )
        self.assertIn(
            BLOCK_EXECUTION_AUTHORIZATION_CONFLICT, conflict_eval.blocking_items
        )
        paths = list(self.authorization_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 16. concurrent authorize --------------------------------------------

    def test_concurrent_authorize_one_success(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        results: list[str] = []
        errors: list[str] = []

        def worker() -> None:
            try:
                summary = self._authorize(activation_id, runtime_invocation_id)
                results.append(summary.authorization_id)
            except ProductionExecutionAuthorizationError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(results)
        self.assertEqual(len(set(results)), 1)
        paths = list(self.authorization_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 17. expired reauthorize blocked (no reissue) --------------------------

    def test_expired_authorization_derived_and_reauthorize_blocked(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        issued = self._authorize(
            activation_id, runtime_invocation_id, ttl_seconds=10
        )
        self.assertEqual(issued.authorization_state, AUTHORIZATION_ISSUED)
        later = self._now + timedelta(seconds=11)
        expired = evaluate_production_execution_authorization(
            **self._auth_kwargs(
                activation_id,
                runtime_invocation_id,
                operator_id="",
                signer_id="",
                now=later,
            )
        )
        self.assertEqual(expired.authorization_state, AUTHORIZATION_EXPIRED)
        with self.assertRaises(ProductionExecutionAuthorizationError) as ctx:
            self._authorize(
                activation_id, runtime_invocation_id, ttl_seconds=10, now=later
            )
        self.assertIn("execution_authorization_expired", str(ctx.exception))
        paths = list(self.authorization_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 18. ExecutionAuthorizationContext -------------------------------------

    def test_context_valid_nested_reuse_and_pickle_blocked(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        issued = self._authorize(activation_id, runtime_invocation_id)
        context = ExecutionAuthorizationContext(
            **self._authorization_context_kwargs(activation_id)
        )
        with context as ctx:
            self.assertTrue(ctx.active)
            self.assertEqual(ctx.authorization_id, issued.authorization_id)
            self.assertEqual(ctx.runtime_invocation_id, runtime_invocation_id)
            self.assertTrue(ctx.execution_phrase_verified)
            with self.assertRaises(ProductionExecutionAuthorizationError):
                with context:
                    pass
        self.assertFalse(context.active)
        with self.assertRaises(ProductionExecutionAuthorizationError):
            with context:
                pass
        with self.assertRaises(ProductionExecutionAuthorizationError):
            pickle.dumps(context)

    def test_context_expired_rejected(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        self._authorize(activation_id, runtime_invocation_id, ttl_seconds=10)
        later = self._now + timedelta(seconds=11)
        context = ExecutionAuthorizationContext(
            **self._authorization_context_kwargs(activation_id, now=later)
        )
        with self.assertRaises(ProductionExecutionAuthorizationError):
            with context:
                pass

    # -- 19. safe output + CLI parsers (+ legacy cutover-check unchanged) -----

    def test_safe_output_and_cli_parsers(self) -> None:
        activation_id, permission_id, session_id, boundary_id, runtime_invocation_id = (
            self._reserve_invocation()
        )
        issued = self._authorize(activation_id, runtime_invocation_id)
        output = format_production_execution_authorization_status(issued)
        self.assertIn("authorization_state:", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("execution_phrase_required: true", output)
        self.assertIn("execution_phrase_verified: true", output)
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE, output)
        self.assertNotIn(_RUNTIME_EXECUTOR, output)
        self.assertNotIn(_AUTHORIZATION_OPERATOR, output)
        self.assertNotIn(_AUTHORIZATION_SIGNER, output)
        self.assertNotIn(str(self.hermes_home), output)
        self.assertNotIn("/opt/data/", output)

        parser = build_coo_dispatch_parser()
        for cmd, handler in (
            (
                [
                    "production", "governed-cutover", "execution-authorization",
                    "status", "--activation-request-id", activation_id,
                ],
                "_cmd_production_execution_authorization_status",
            ),
            (
                [
                    "production", "governed-cutover", "execution-authorization",
                    "check",
                    "--activation-request-id", activation_id,
                    "--runtime-invocation-id", runtime_invocation_id,
                ],
                "_cmd_production_execution_authorization_check",
            ),
            (
                [
                    "production", "governed-cutover", "execution-authorization",
                    "authorize",
                    "--activation-request-id", activation_id,
                    "--runtime-invocation-id", runtime_invocation_id,
                    "--executor-id", _RUNTIME_EXECUTOR,
                    "--operator-id", _AUTHORIZATION_OPERATOR,
                    "--signer-id", _AUTHORIZATION_SIGNER,
                    "--ttl-seconds", "15",
                    "--phrase", REQUIRED_CONFIRMATION_PHRASE,
                ],
                "_cmd_production_execution_authorization_authorize",
            ),
            (
                [
                    "production", "governed-cutover", "execution-authorization",
                    "show", "--authorization-id", issued.authorization_id,
                ],
                "_cmd_production_execution_authorization_show",
            ),
            (
                [
                    "production", "governed-cutover", "execution-authorization",
                    "history", "--activation-request-id", activation_id,
                ],
                "_cmd_production_execution_authorization_history",
            ),
        ):
            args = parser.parse_args(cmd)
            self.assertEqual(args.handler.__name__, handler)

        legacy = parser.parse_args(
            ["production", "cutover-check", "--ticket-id", "ticket-x"]
        )
        self.assertEqual(legacy.handler.__name__, "_cmd_production_cutover_check")
        invocation_status = parser.parse_args(
            [
                "production", "governed-cutover", "runtime-invocation", "status",
                "--activation-request-id", activation_id,
            ]
        )
        self.assertEqual(
            invocation_status.handler.__name__,
            "_cmd_production_runtime_invocation_status",
        )

    # -- 20. dashboard + release summary ---------------------------------------

    def test_dashboard_and_release_summary(self) -> None:
        activation_id, permission_id, session_id, boundary_id, runtime_invocation_id = (
            self._reserve_invocation()
        )
        issued = self._authorize(activation_id, runtime_invocation_id)

        digest = resolve_latest_execution_authorization_dashboard_digest(
            governed_cutover_store_dir=self.governed_cutover_store_dir,
            window_store_dir=self.window_store_dir,
            permission_store_dir=self.permission_store_dir,
            session_store_dir=self.session_store_dir,
            boundary_store_dir=self.boundary_store_dir,
            invocation_store_dir=self.invocation_store_dir,
            authorization_store_dir=self.authorization_store_dir,
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
        self.assertEqual(digest.execution_authorization_state, AUTHORIZATION_ISSUED)
        self.assertTrue(digest.execution_authorization_present)
        self.assertEqual(
            digest.execution_authorization_id, issued.authorization_id
        )
        self.assertTrue(digest.execution_authorization_phrase_verified)

        dashboard = build_operator_dashboard_summary(merged_config={})
        self.assertTrue(hasattr(dashboard, "execution_authorization_state"))
        self.assertTrue(hasattr(dashboard, "governed_runtime_invocation_state"))

        release = build_production_execution_authorization_release_summary(issued)
        self.assertEqual(
            release.release_status, RELEASE_EXECUTION_AUTHORIZATION_ISSUED
        )
        self.assertEqual(release.next_phase, "Phase_15H_governed_runtime_start")
        self.assertFalse(release.production_execution_allowed)
        self.assertFalse(release.cutover_started)
        self.assertFalse(release.runtime_invoked)
        self.assertFalse(release.permission_consumed)
        self.assertFalse(release.permission_revoked)
        self.assertTrue(release.execution_phrase_required)
        self.assertTrue(release.execution_phrase_verified)

        contract = load_governed_cutover_contract(
            activation_id, store_dir=self.governed_cutover_store_dir
        )
        assert contract is not None
        self.assertFalse(contract.cutover_started)

        by_id = load_execution_authorization_by_id(
            issued.authorization_id, store_dir=self.authorization_store_dir
        )
        assert by_id is not None
        self.assertEqual(by_id.authorization_id, issued.authorization_id)

    # -- 21. no subprocess on authorize -----------------------------------------

    def test_no_subprocess_on_authorize(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id = self._reserve_invocation()
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                self._authorize(activation_id, runtime_invocation_id)

    # -- 22. boundary store dir default path ------------------------------------

    def test_default_store_dir_is_hermes_home_scoped(self) -> None:
        default_dir = default_execution_authorization_store_dir()
        self.assertTrue(str(default_dir).endswith(
            "coo/production-execution-authorization"
        ))


if __name__ == "__main__":
    unittest.main()
