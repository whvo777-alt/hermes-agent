"""Phase 15J tests — governed runtime closure & consistency validation."""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import timedelta
from unittest.mock import patch

from agent.coo.production_controlled_window import (
    REASON_OPERATOR_CLOSE,
    WINDOW_CLOSED,
    WINDOW_OPEN,
    close_production_controlled_window,
)
from agent.coo.production_execution_authorization import (
    load_execution_authorization_consume_record,
    load_execution_authorization_record,
)
from agent.coo.production_governed_runtime_closure import (
    BLOCK_AUTHORIZATION_CONSUME_MISSING,
    BLOCK_BOUNDARY_CONSUME_MISSING,
    BLOCK_CLOSURE_CONFLICT,
    BLOCK_CONSUME_ORDER_INVALID,
    BLOCK_CONSUME_REPLAY_DETECTED,
    BLOCK_CORRELATION_INVALID,
    BLOCK_GOVERNED_RUNTIME_INVOKE_MISSING,
    BLOCK_INVOCATION_CONSUME_MISSING,
    BLOCK_PARTIAL_CONSUME_DETECTED,
    BLOCK_PERMISSION_CONSUME_MISSING,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_REPAIR_LOCK_HELD,
    BLOCK_RUNTIME_START_MISSING,
    CLOSURE_BLOCKED,
    CLOSURE_COMPLETED,
    CLOSURE_NOT_READY,
    CLOSURE_READY,
    CLOSURE_REQUIRES_RECOVERY,
    ProductionGovernedRuntimeClosureError,
    build_production_governed_runtime_closure_audit_summary,
    build_production_governed_runtime_closure_release_summary,
    evaluate_production_governed_runtime_closure,
    format_production_governed_runtime_closure,
    load_production_governed_runtime_closure,
    record_production_governed_runtime_closure,
)
from agent.coo.production_governed_runtime_invoke import (
    reserve_and_consume_governed_runtime_invoke,
)
from agent.coo.production_runtime_boundary import load_runtime_boundary_consume_record
from agent.coo.production_runtime_consume_store import write_once_consume_record
from agent.coo.production_runtime_invocation import load_runtime_invocation_consume_record
from agent.coo.production_runtime_permission import load_runtime_permission_consume_record
from tests.hermes_cli.test_production_governed_runtime_invoke import (
    TestProductionGovernedRuntimeInvoke,
)

_CLOSURE_OPERATOR = "governed-closure-operator-phase15j"


class TestProductionGovernedRuntimeClosure(TestProductionGovernedRuntimeInvoke):
    def setUp(self) -> None:
        super().setUp()
        self.closure_store_dir = (
            self.hermes_home / "coo" / "production-governed-runtime-closure"
        )
        self.closure_store_dir.mkdir(parents=True, exist_ok=True)

    def _closure_kwargs(self, activation_id: str, authorization_id: str, **overrides):
        base = {
            k: v
            for k, v in self._invoke_kwargs(activation_id, authorization_id).items()
            if k not in ("activation_request_id",)
        }
        base["closure_store_dir"] = self.closure_store_dir
        base.update(overrides)
        return base

    def _invoked_chain(self):
        activation_id, permission_id, session_id, boundary_id, runtime_invocation_id, authorization_id = (
            self._ready_chain()
        )
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no popen")),
        ):
            reserve_and_consume_governed_runtime_invoke(
                activation_id,
                invoked_by=_CLOSURE_OPERATOR,
                **{
                    k: v
                    for k, v in self._invoke_kwargs(activation_id, authorization_id).items()
                    if k not in ("activation_request_id",)
                },
            )
        return (
            activation_id,
            permission_id,
            session_id,
            boundary_id,
            runtime_invocation_id,
            authorization_id,
        )

    # -- 1/2. full chain ready + record completed --------------------------

    def test_closure_ready_after_full_chain(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertEqual(summary.closure_state, CLOSURE_READY)
        self.assertTrue(summary.closure_ready)
        self.assertTrue(summary.chain_complete)
        self.assertTrue(summary.consume_chain_complete)
        self.assertTrue(summary.correlation_valid)
        self.assertFalse(summary.replay_detected)
        self.assertFalse(summary.partial_consume_detected)
        self.assertEqual(summary.blocking_items, ())

    def test_record_closure_completed(self) -> None:
        activation_id, permission_id, session_id, boundary_id, runtime_invocation_id, authorization_id = (
            self._invoked_chain()
        )
        record = record_production_governed_runtime_closure(
            activation_id, **self._closure_kwargs(activation_id, authorization_id)
        )
        self.assertEqual(record.closure_status, CLOSURE_COMPLETED)
        self.assertTrue(record.governed_runtime_invoked)
        self.assertTrue(record.consume_chain_complete)
        self.assertTrue(record.correlation_valid)
        self.assertEqual(record.permission_id, permission_id)
        self.assertEqual(record.session_id, session_id)
        self.assertEqual(record.boundary_id, boundary_id)
        self.assertEqual(record.runtime_invocation_id, runtime_invocation_id)
        self.assertEqual(record.authorization_id, authorization_id)
        self.assertFalse(record.production_execution_allowed)
        self.assertFalse(record.original_repository2_execution_attempted)

    # -- 3/4. append-only + upstream immutability ----------------------------

    def test_closure_artifact_write_once_and_upstream_bytes_unchanged(self) -> None:
        activation_id, permission_id, _s, boundary_id, _inv, authorization_id = (
            self._invoked_chain()
        )
        permission_path = self.permission_store_dir / f"{activation_id}.json"
        boundary_path = self.boundary_store_dir / f"{activation_id}.json"
        runtime_start_path = self.runtime_start_store_dir / f"{activation_id}.json"
        before_permission = permission_path.read_bytes()
        before_boundary = boundary_path.read_bytes()
        before_runtime_start = runtime_start_path.read_bytes()

        kwargs = self._closure_kwargs(activation_id, authorization_id)
        record_production_governed_runtime_closure(activation_id, **kwargs)

        closure_path = self.closure_store_dir / f"{activation_id}.json"
        self.assertTrue(closure_path.is_file())
        before_closure = closure_path.read_bytes()

        # Re-recording (idempotent path) must not touch the file at all.
        record_production_governed_runtime_closure(activation_id, **kwargs)
        self.assertEqual(closure_path.read_bytes(), before_closure)

        self.assertEqual(permission_path.read_bytes(), before_permission)
        self.assertEqual(boundary_path.read_bytes(), before_boundary)
        self.assertEqual(runtime_start_path.read_bytes(), before_runtime_start)

    # -- 6/7/8. runtime meaning separation ------------------------------------

    def test_runtime_meaning_separation_fields_distinct(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertTrue(summary.governed_runtime_invoked)
        # This test's fixture chain exercises the isolated-mirror live
        # runtime step, so both Phase 14 signals should be True while
        # remaining explicitly separate fields from governed_runtime_invoked.
        self.assertTrue(summary.phase14_runtime_invoked)
        self.assertTrue(summary.isolated_mirror_runtime_invoked)
        self.assertFalse(summary.original_repository2_execution_attempted)
        record = record_production_governed_runtime_closure(
            activation_id, **self._closure_kwargs(activation_id, authorization_id)
        )
        payload = record.to_dict()
        self.assertNotIn("runtime_invoked", payload)
        self.assertIn("governed_runtime_invoked", payload)
        self.assertFalse(payload["original_repository2_execution_attempted"])

    def test_safe_output_no_ambiguous_runtime_key_or_secrets(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        output = format_production_governed_runtime_closure(summary)
        # The bare, ambiguous "runtime_invoked" key must never appear on its
        # own line — only qualified variants (governed_/phase14_/isolated_
        # mirror_) are allowed. Check per-line rather than substring, since
        # e.g. "phase14_runtime_invoked:" legitimately contains
        # "runtime_invoked:" as a suffix.
        lines = output.splitlines()
        self.assertNotIn("runtime_invoked", [line.split(":", 1)[0] for line in lines])
        self.assertIn("governed_runtime_invoked:", output)
        self.assertIn("phase14_runtime_invoked:", output)
        self.assertIn("isolated_mirror_runtime_invoked:", output)
        for forbidden in (
            "password",
            "secret",
            "phrase",
            "argv",
            "stdout",
            "stderr",
            "executor_id",
            "operator_id",
            "/opt/data/multi-content-pipeline",
        ):
            self.assertNotIn(forbidden, output.lower())

    # -- 9/10. partial consume: each single missing + N/4 combinations -------

    def _delete_consume(self, path: "object") -> None:
        path.unlink()

    def test_permission_consume_missing_requires_recovery(self) -> None:
        activation_id, permission_id, _s, _b, _inv, authorization_id = self._invoked_chain()
        (self.permission_consume_store_dir / f"{permission_id}.json").unlink()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertEqual(summary.closure_state, CLOSURE_REQUIRES_RECOVERY)
        self.assertIn(BLOCK_PERMISSION_CONSUME_MISSING, summary.blocking_items)
        self.assertIn(BLOCK_PARTIAL_CONSUME_DETECTED, summary.blocking_items)
        with self.assertRaises(ProductionGovernedRuntimeClosureError):
            record_production_governed_runtime_closure(
                activation_id, **self._closure_kwargs(activation_id, authorization_id)
            )
        self.assertIsNone(
            load_production_governed_runtime_closure(
                activation_id, store_dir=self.closure_store_dir
            )
        )

    def test_boundary_consume_missing_requires_recovery(self) -> None:
        activation_id, _p, _s, boundary_id, _inv, authorization_id = self._invoked_chain()
        (self.boundary_consume_store_dir / f"{boundary_id}.json").unlink()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertEqual(summary.closure_state, CLOSURE_REQUIRES_RECOVERY)
        self.assertIn(BLOCK_BOUNDARY_CONSUME_MISSING, summary.blocking_items)

    def test_invocation_consume_missing_requires_recovery(self) -> None:
        activation_id, _p, _s, _b, runtime_invocation_id, authorization_id = (
            self._invoked_chain()
        )
        (self.invocation_consume_store_dir / f"{runtime_invocation_id}.json").unlink()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertEqual(summary.closure_state, CLOSURE_REQUIRES_RECOVERY)
        self.assertIn(BLOCK_INVOCATION_CONSUME_MISSING, summary.blocking_items)

    def test_authorization_consume_missing_requires_recovery(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        (self.authorization_consume_store_dir / f"{authorization_id}.json").unlink()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertEqual(summary.closure_state, CLOSURE_REQUIRES_RECOVERY)
        self.assertIn(BLOCK_AUTHORIZATION_CONSUME_MISSING, summary.blocking_items)
        self.assertEqual(
            summary.recommended_action, "inspect_partial_governed_consume"
        )

    def test_two_of_four_consume_missing(self) -> None:
        activation_id, permission_id, _s, boundary_id, _inv, authorization_id = (
            self._invoked_chain()
        )
        (self.permission_consume_store_dir / f"{permission_id}.json").unlink()
        (self.boundary_consume_store_dir / f"{boundary_id}.json").unlink()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertEqual(summary.closure_state, CLOSURE_REQUIRES_RECOVERY)
        self.assertIn(BLOCK_PERMISSION_CONSUME_MISSING, summary.blocking_items)
        self.assertIn(BLOCK_BOUNDARY_CONSUME_MISSING, summary.blocking_items)

    def test_three_of_four_consume_missing(self) -> None:
        activation_id, permission_id, _s, boundary_id, runtime_invocation_id, authorization_id = (
            self._invoked_chain()
        )
        (self.permission_consume_store_dir / f"{permission_id}.json").unlink()
        (self.boundary_consume_store_dir / f"{boundary_id}.json").unlink()
        (self.invocation_consume_store_dir / f"{runtime_invocation_id}.json").unlink()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertEqual(summary.closure_state, CLOSURE_REQUIRES_RECOVERY)
        self.assertTrue(summary.partial_consume_detected)

    # -- 11/12. consume order / timestamp reversal ----------------------------

    def test_consume_order_invalid_on_timestamp_reversal(self) -> None:
        activation_id, permission_id, _s, _b, _inv, authorization_id = self._invoked_chain()
        path = self.permission_consume_store_dir / f"{permission_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Rewrite permission's own consumed_at to be AFTER authorization's,
        # simulating a corrupted/out-of-order artifact. Direct low-level
        # rewrite (not overwrite of the write-once helper's guarantees) is
        # done here only to construct the test fixture, mirroring how other
        # phases in this codebase hand-craft corrupted fixtures.
        payload["consumed_at"] = "2099-01-01T00:00:00+00:00"
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertIn(BLOCK_CONSUME_ORDER_INVALID, summary.blocking_items)
        self.assertEqual(summary.closure_state, CLOSURE_REQUIRES_RECOVERY)

    # -- 13. consume correlation mismatch --------------------------------------

    def test_consume_correlation_mismatch_blocks(self) -> None:
        activation_id, permission_id, _s, _b, _inv, authorization_id = self._invoked_chain()
        path = self.permission_consume_store_dir / f"{permission_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cutover_contract_id"] = "wrong-contract-id"
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertIn(BLOCK_CORRELATION_INVALID, summary.blocking_items)
        self.assertEqual(summary.closure_state, CLOSURE_BLOCKED)
        self.assertFalse(summary.correlation_valid)

    # -- 14/15. replay detection -----------------------------------------------

    def test_replay_detected_when_consume_references_different_invoke(self) -> None:
        activation_id, permission_id, _s, _b, _inv, authorization_id = self._invoked_chain()
        path = self.permission_consume_store_dir / f"{permission_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["governed_invoke_id"] = str(uuid.uuid4())
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertTrue(summary.replay_detected)
        self.assertIn(BLOCK_CONSUME_REPLAY_DETECTED, summary.blocking_items)
        self.assertEqual(summary.closure_state, CLOSURE_BLOCKED)
        self.assertEqual(
            summary.recommended_action, "resolve_governed_runtime_replay"
        )
        with self.assertRaises(ProductionGovernedRuntimeClosureError):
            record_production_governed_runtime_closure(
                activation_id, **self._closure_kwargs(activation_id, authorization_id)
            )

    # -- 16/17. missing governed invoke / missing runtime start --------------

    def test_runtime_start_missing_blocks_closure(self) -> None:
        (
            activation_id,
            _permission_id,
            _session_id,
            _boundary_id,
            _runtime_invocation_id,
            authorization_id,
        ) = self._authorize_chain()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertIn(BLOCK_RUNTIME_START_MISSING, summary.blocking_items)
        self.assertIn(BLOCK_GOVERNED_RUNTIME_INVOKE_MISSING, summary.blocking_items)
        self.assertEqual(summary.closure_state, CLOSURE_NOT_READY)
        self.assertFalse(summary.closure_ready)

    def test_missing_governed_invoke_record_but_runtime_started(self) -> None:
        (
            activation_id,
            _permission_id,
            _session_id,
            _boundary_id,
            _runtime_invocation_id,
            authorization_id,
        ) = self._ready_chain()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertIn(BLOCK_GOVERNED_RUNTIME_INVOKE_MISSING, summary.blocking_items)
        self.assertFalse(summary.closure_ready)

    # -- 19/20. window open / closed -------------------------------------------

    def test_window_open_requires_close_warning(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertEqual(summary.window_state, WINDOW_OPEN)
        self.assertTrue(summary.window_close_required)
        self.assertIn("window_close_required", summary.warning_items)

    def test_window_closed_no_close_warning(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        close_production_controlled_window(
            activation_request_id=activation_id,
            operator_id=_CLOSURE_OPERATOR,
            reason_code=REASON_OPERATOR_CLOSE,
            **{
                k: v
                for k, v in self._eval_kwargs(activation_id).items()
                if k != "activation_request_id" and k != "operator_id"
            },
        )
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        self.assertEqual(summary.window_state, WINDOW_CLOSED)
        self.assertFalse(summary.window_close_required)
        self.assertNotIn("window_close_required", summary.warning_items)
        # Closure itself must still be recordable once the window is closed.
        record = record_production_governed_runtime_closure(
            activation_id, **self._closure_kwargs(activation_id, authorization_id)
        )
        self.assertEqual(record.closure_status, CLOSURE_COMPLETED)

    # -- 21. recovery_required / repair_lock_held -> blocked -------------------

    def test_recovery_required_blocks_closure(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        import agent.coo.production_governed_runtime_closure as closure_mod
        from agent.coo.production_controlled_window import (
            ProductionControlledWindowSummary,
        )

        real_summary = closure_mod.evaluate_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=""),
        )
        forced = ProductionControlledWindowSummary(
            **{**real_summary.__dict__, "recovery_required": True}
        )
        with patch.object(
            closure_mod, "evaluate_production_controlled_window", return_value=forced
        ):
            summary = evaluate_production_governed_runtime_closure(
                activation_request_id=activation_id,
                **self._closure_kwargs(activation_id, authorization_id),
            )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, summary.blocking_items)
        self.assertEqual(summary.closure_state, CLOSURE_BLOCKED)
        self.assertEqual(summary.recommended_action, "run_consume_recovery")

    def test_repair_lock_held_blocks_closure(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        import agent.coo.production_governed_runtime_closure as closure_mod
        from agent.coo.production_controlled_window import (
            ProductionControlledWindowSummary,
        )

        real_summary = closure_mod.evaluate_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=""),
        )
        forced = ProductionControlledWindowSummary(
            **{**real_summary.__dict__, "repair_lock_held": True}
        )
        with patch.object(
            closure_mod, "evaluate_production_controlled_window", return_value=forced
        ):
            summary = evaluate_production_governed_runtime_closure(
                activation_request_id=activation_id,
                **self._closure_kwargs(activation_id, authorization_id),
            )
        self.assertIn(BLOCK_REPAIR_LOCK_HELD, summary.blocking_items)
        self.assertEqual(summary.closure_state, CLOSURE_BLOCKED)

    # -- 22. production flags true -> blocked -----------------------------------

    def test_production_execution_allowed_true_blocks(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
            force_production_execution_allowed=True,
        )
        self.assertEqual(summary.closure_state, CLOSURE_BLOCKED)
        self.assertFalse(summary.closure_ready)
        with self.assertRaises(ProductionGovernedRuntimeClosureError):
            record_production_governed_runtime_closure(
                activation_id,
                **self._closure_kwargs(activation_id, authorization_id),
                force_production_execution_allowed=True,
            )

    def test_gateway_and_discord_enabled_block(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
            force_gateway_enabled=True,
        )
        self.assertEqual(summary.closure_state, CLOSURE_BLOCKED)
        summary2 = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
            force_discord_enabled=True,
        )
        self.assertEqual(summary2.closure_state, CLOSURE_BLOCKED)

    # -- 23/24. idempotent duplicate / mismatched conflict ---------------------

    def test_duplicate_closure_call_is_idempotent(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        kwargs = self._closure_kwargs(activation_id, authorization_id)
        first = record_production_governed_runtime_closure(activation_id, **kwargs)
        second = record_production_governed_runtime_closure(activation_id, **kwargs)
        self.assertEqual(first.closure_id, second.closure_id)

    def test_mismatched_duplicate_closure_conflicts(self) -> None:
        activation_id, permission_id, session_id, boundary_id, runtime_invocation_id, authorization_id = (
            self._invoked_chain()
        )
        # Hand-craft a pre-existing closure artifact with a mismatched
        # permission_id to simulate a conflicting prior closure.
        fake_payload = {
            "closure_id": str(uuid.uuid4()),
            "activation_request_id": activation_id,
            "cutover_contract_id": "x",
            "permission_id": "mismatched-permission-id",
            "session_id": session_id,
            "boundary_id": boundary_id,
            "runtime_invocation_id": runtime_invocation_id,
            "authorization_id": authorization_id,
            "runtime_start_id": "x",
            "governed_runtime_invoke_id": "x",
            "permission_consume_record_id": "",
            "boundary_consume_record_id": "",
            "invocation_consume_record_id": "",
            "authorization_consume_record_id": "",
            "reservation_id": "",
            "execution_attempt_id": "",
            "dispatch_run_id": "",
            "ticket_id": "",
            "confirmation_id": "",
            "closure_status": CLOSURE_COMPLETED,
            "correlation_valid": True,
            "consume_chain_complete": True,
            "governed_runtime_invoked": True,
            "runtime_started": True,
            "original_repository2_execution_attempted": False,
            "production_execution_allowed": False,
            "production_root_hard_deny": True,
            "external_publish_enabled": False,
            "gateway_production_enabled": False,
            "discord_production_enabled": False,
            "completed_at": "2026-01-01T00:00:00+00:00",
            "tested_commit_sha": "x",
            "release_tag": "x",
            "warning_codes": [],
            "blocking_codes": [],
        }
        write_once_consume_record(
            self.closure_store_dir / f"{activation_id}.json", fake_payload
        )
        with self.assertRaises(ProductionGovernedRuntimeClosureError) as exc:
            record_production_governed_runtime_closure(
                activation_id, **self._closure_kwargs(activation_id, authorization_id)
            )
        self.assertIn(BLOCK_CLOSURE_CONFLICT, str(exc.exception))

    # -- 25. corrupted closure -> fail-closed -----------------------------------

    def test_corrupted_closure_fail_closed(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        path = self.closure_store_dir / f"{activation_id}.json"
        path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(ProductionGovernedRuntimeClosureError):
            load_production_governed_runtime_closure(
                activation_id, store_dir=self.closure_store_dir
            )
        with self.assertRaises(ProductionGovernedRuntimeClosureError):
            evaluate_production_governed_runtime_closure(
                activation_request_id=activation_id,
                **self._closure_kwargs(activation_id, authorization_id),
            )

    # -- 26. release / audit summaries ------------------------------------------

    def test_release_and_audit_summaries(self) -> None:
        activation_id, _p, _s, _b, _inv, authorization_id = self._invoked_chain()
        record_production_governed_runtime_closure(
            activation_id, **self._closure_kwargs(activation_id, authorization_id)
        )
        summary = evaluate_production_governed_runtime_closure(
            activation_request_id=activation_id,
            **self._closure_kwargs(activation_id, authorization_id),
        )
        release = build_production_governed_runtime_closure_release_summary(summary)
        self.assertEqual(release.release_status, "GOVERNED_RUNTIME_CLOSURE_COMPLETED")
        self.assertTrue(release.closure_completed)
        self.assertEqual(
            release.next_phase, "prepare_phase_16_v1_release_candidate_validation"
        )
        self.assertFalse(release.production_execution_allowed)
        self.assertFalse(release.original_repository2_execution_attempted)

        audit = build_production_governed_runtime_closure_audit_summary(summary)
        self.assertEqual(audit.consume_record_count, 4)
        self.assertEqual(audit.replay_count, 0)
        self.assertEqual(audit.partial_consume_count, 0)
        self.assertEqual(audit.mismatch_count, 0)

    # -- 27/28/29/30. static safety guards ---------------------------------------

    def test_module_has_no_dangerous_execution_surface(self) -> None:
        import agent.coo.production_governed_runtime_closure as mod

        with open(mod.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("subprocess.run(", source)
        self.assertNotIn("subprocess.Popen(", source)
        self.assertNotIn("bounded_subprocess_runner", source)
        self.assertNotIn("create_bounded_subprocess_runner", source)
        self.assertNotIn("PipelineAdapter", source)
        # The literal Repository2 path is allowed to appear exactly once —
        # as a string the safe-output guard checks *against* — but must
        # never be used to construct an actual filesystem path/open call.
        self.assertEqual(source.count("/opt/data/multi-content-pipeline"), 1)
        self.assertNotIn('Path("/opt/data/multi-content-pipeline")', source)
        self.assertNotIn("open(\"/opt/data/multi-content-pipeline", source)

    def test_no_cli_wiring(self) -> None:
        with open("hermes_cli/coo_dispatch.py", encoding="utf-8") as handle:
            cli_source = handle.read()
        self.assertNotIn("production_governed_runtime_closure", cli_source)
        self.assertNotIn("governed_runtime_closure", cli_source)


if __name__ == "__main__":
    import unittest

    unittest.main()
