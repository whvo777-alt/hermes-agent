"""Phase 15C tests — production runtime permission contract."""

from __future__ import annotations

import hashlib
import json
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
    WINDOW_OPEN,
    close_production_controlled_window,
    emergency_close_production_controlled_window,
    open_production_controlled_window,
)
from agent.coo.production_governed_cutover import (
    load_governed_cutover_contract,
)
from agent.coo.production_runtime_permission import (
    ACTION_ISSUE_PRODUCTION_RUNTIME_PERMISSION,
    ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT,
    ACTION_PREPARE_PHASE_15D_GOVERNED_RUNTIME_SESSION,
    BLOCK_CONTROLLED_WINDOW_CLOSED,
    BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED,
    BLOCK_CONTROLLED_WINDOW_EXPIRED,
    BLOCK_CONTROLLED_WINDOW_NOT_OPEN,
    BLOCK_EXECUTOR_IDENTITY_INVALID,
    BLOCK_IDENTITY_SEPARATION_INVALID,
    BLOCK_OPERATOR_IDENTITY_INVALID,
    BLOCK_PERMISSION_EXPIRED,
    BLOCK_PRODUCTION_EXECUTION_ENABLED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_TTL_EXCEEDS_WINDOW,
    BLOCK_TTL_INVALID,
    PERMISSION_EXPIRED,
    PERMISSION_ISSUED,
    PERMISSION_READY,
    RELEASE_RUNTIME_PERMISSION_ISSUED,
    ProductionRuntimePermissionError,
    build_production_runtime_permission_release_summary,
    evaluate_production_runtime_permission,
    format_production_runtime_permission_status,
    issue_production_runtime_permission,
    load_runtime_permission_events,
    load_runtime_permission_record,
    resolve_latest_runtime_permission_dashboard_digest,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.test_production_controlled_window import (
    TestProductionControlledWindow as _ControlledWindowBase,
    _WINDOW_OPERATOR,
)
from tests.hermes_cli.test_production_final_signoff import _FINAL_SIGNER
from tests.hermes_cli.test_production_governed_cutover import _CUTOVER_OPERATOR

_RUNTIME_EXECUTOR = "runtime-executor-phase15c"
_PERMISSION_OPERATOR = "permission-operator-phase15c"


class TestProductionRuntimePermission(_ControlledWindowBase):
    def setUp(self) -> None:
        super().setUp()
        self.permission_store_dir = (
            self.hermes_home / "coo" / "production-runtime-permission"
        )
        self.permission_store_dir.mkdir(parents=True, exist_ok=True)

    def _open_window(self) -> str:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        return activation_id

    def _perm_kwargs(self, activation_id: str, **overrides):
        base = {
            **self._eval_kwargs(activation_id),
            "permission_store_dir": self.permission_store_dir,
            "executor_id": _RUNTIME_EXECUTOR,
            "operator_id": _PERMISSION_OPERATOR,
            "ttl_seconds": 300,
        }
        base.update(overrides)
        return base

    def _digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_check_ready_when_window_open(self) -> None:
        activation_id = self._open_window()
        summary = evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id, operator_id="")
        )
        self.assertEqual(summary.permission_state, PERMISSION_READY)
        self.assertTrue(summary.permission_ready)
        self.assertEqual(
            summary.recommended_action,
            ACTION_ISSUE_PRODUCTION_RUNTIME_PERMISSION,
        )
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.runtime_invoked)

    def test_check_read_only_digest_unchanged(self) -> None:
        activation_id = self._open_window()
        contract_path = self.governed_cutover_store_dir / f"{activation_id}.json"
        window_path = self.window_store_dir / f"{activation_id}.json"
        activation_path = self.store_dir / f"{activation_id}.json"
        before = (
            self._digest(contract_path),
            self._digest(window_path),
            self._digest(activation_path),
        )
        evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id, operator_id="")
        )
        after = (
            self._digest(contract_path),
            self._digest(window_path),
            self._digest(activation_path),
        )
        self.assertEqual(before, after)
        self.assertFalse(list(self.permission_store_dir.glob("*.json")))

    def test_issue_success_immutable_artifact(self) -> None:
        activation_id = self._open_window()
        contract_path = self.governed_cutover_store_dir / f"{activation_id}.json"
        window_path = self.window_store_dir / f"{activation_id}.json"
        activation_path = self.store_dir / f"{activation_id}.json"
        before = (
            self._digest(contract_path),
            self._digest(window_path),
            self._digest(activation_path),
        )
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                summary = issue_production_runtime_permission(
                    **self._perm_kwargs(activation_id)
                )
        after = (
            self._digest(contract_path),
            self._digest(window_path),
            self._digest(activation_path),
        )
        self.assertEqual(before, after)
        self.assertEqual(summary.permission_state, PERMISSION_ISSUED)
        self.assertTrue(summary.permission_present)
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.runtime_invoked)
        self.assertTrue(summary.production_root_hard_deny)
        self.assertEqual(
            summary.recommended_action,
            ACTION_PREPARE_PHASE_15D_GOVERNED_RUNTIME_SESSION,
        )
        record = load_runtime_permission_record(
            activation_id, store_dir=self.permission_store_dir
        )
        assert record is not None
        self.assertEqual(record.max_executions, 1)
        self.assertEqual(record.execution_count, 0)
        self.assertFalse(record.consumed)
        self.assertFalse(record.revoked)
        self.assertEqual(record.scope_type, "one_shot")
        events = load_runtime_permission_events(
            activation_id, store_dir=self.permission_store_dir
        )
        types = [event.event_type for event in events]
        self.assertIn("permission_issue_requested", types)
        self.assertIn("permission_issued", types)

    def test_window_not_open_blocked(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        summary = evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id)
        )
        self.assertIn(BLOCK_CONTROLLED_WINDOW_NOT_OPEN, summary.blocking_items)
        with self.assertRaises(ProductionRuntimePermissionError):
            issue_production_runtime_permission(**self._perm_kwargs(activation_id))

    def test_closed_window_blocked(self) -> None:
        activation_id = self._open_window()
        close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                reason_code=REASON_OPERATOR_CLOSE,
            )
        )
        summary = evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id)
        )
        self.assertIn(BLOCK_CONTROLLED_WINDOW_CLOSED, summary.blocking_items)

    def test_emergency_closed_blocked(self) -> None:
        activation_id = self._open_window()
        emergency_close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                actor_role="operator",
                reason_code=REASON_INCIDENT_DETECTED,
            )
        )
        summary = evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id)
        )
        self.assertIn(BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED, summary.blocking_items)

    def test_expired_window_blocked(self) -> None:
        activation_id = self._open_window()
        later = self._now + timedelta(hours=2)
        summary = evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id, now=later)
        )
        self.assertTrue(summary.controlled_window_expired)
        self.assertIn(BLOCK_CONTROLLED_WINDOW_EXPIRED, summary.blocking_items)

    def test_ttl_bounds(self) -> None:
        activation_id = self._open_window()
        low = evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id, ttl_seconds=30)
        )
        self.assertIn(BLOCK_TTL_INVALID, low.blocking_items)
        high = evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id, ttl_seconds=901)
        )
        self.assertIn(BLOCK_TTL_INVALID, high.blocking_items)
        exceeds = evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id, ttl_seconds=900)
        )
        # Active window remainder ~50m; 900s may still fit.
        # Force exceed with huge remaining gap via near-end window clock.
        near_end = self._now + timedelta(minutes=49)
        over = evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id, ttl_seconds=300, now=near_end)
        )
        self.assertIn(BLOCK_TTL_EXCEEDS_WINDOW, over.blocking_items)
        with self.assertRaises(ProductionRuntimePermissionError):
            issue_production_runtime_permission(
                **self._perm_kwargs(activation_id, ttl_seconds=30)
            )

    def test_identity_conflicts(self) -> None:
        activation_id = self._open_window()
        for bad_executor in (
            "operator-a",
            _CUTOVER_OPERATOR,
            _FINAL_SIGNER,
            "operator-supervisor",
        ):
            summary = evaluate_production_runtime_permission(
                **self._perm_kwargs(activation_id, executor_id=bad_executor)
            )
            self.assertIn(
                BLOCK_EXECUTOR_IDENTITY_INVALID,
                summary.blocking_items,
                msg=bad_executor,
            )
        same = evaluate_production_runtime_permission(
            **self._perm_kwargs(
                activation_id,
                executor_id=_RUNTIME_EXECUTOR,
                operator_id=_RUNTIME_EXECUTOR,
            )
        )
        self.assertTrue(
            BLOCK_IDENTITY_SEPARATION_INVALID in same.blocking_items
            or BLOCK_OPERATOR_IDENTITY_INVALID in same.blocking_items
        )
        op_conflict = evaluate_production_runtime_permission(
            **self._perm_kwargs(
                activation_id,
                operator_id=_FINAL_SIGNER,
            )
        )
        self.assertIn(BLOCK_OPERATOR_IDENTITY_INVALID, op_conflict.blocking_items)

    def test_recovery_and_repair_lock_blocked(self) -> None:
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
            summary = evaluate_production_runtime_permission(
                **self._perm_kwargs(activation_id)
            )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, summary.blocking_items)

    def test_force_flags_and_signoff_blocks(self) -> None:
        activation_id = self._open_window()
        forced = evaluate_production_runtime_permission(
            **self._perm_kwargs(
                activation_id,
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

    def test_duplicate_same_issue_idempotent(self) -> None:
        activation_id = self._open_window()
        first = issue_production_runtime_permission(**self._perm_kwargs(activation_id))
        second = issue_production_runtime_permission(**self._perm_kwargs(activation_id))
        self.assertEqual(first.permission_id, second.permission_id)
        self.assertEqual(second.permission_state, PERMISSION_ISSUED)
        paths = list(self.permission_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    def test_duplicate_changed_executor_conflict(self) -> None:
        activation_id = self._open_window()
        issue_production_runtime_permission(**self._perm_kwargs(activation_id))
        with self.assertRaises(ProductionRuntimePermissionError) as ctx:
            issue_production_runtime_permission(
                **self._perm_kwargs(
                    activation_id,
                    executor_id="other-runtime-executor",
                )
            )
        self.assertIn("runtime_permission_conflict", str(ctx.exception))

    def test_duplicate_changed_ttl_conflict(self) -> None:
        activation_id = self._open_window()
        issue_production_runtime_permission(**self._perm_kwargs(activation_id))
        with self.assertRaises(ProductionRuntimePermissionError) as ctx:
            issue_production_runtime_permission(
                **self._perm_kwargs(activation_id, ttl_seconds=120)
            )
        self.assertIn("runtime_permission_conflict", str(ctx.exception))

    def test_concurrent_issue_one_success(self) -> None:
        activation_id = self._open_window()
        results: list[str] = []
        errors: list[str] = []

        def worker() -> None:
            try:
                summary = issue_production_runtime_permission(
                    **self._perm_kwargs(activation_id)
                )
                results.append(summary.permission_id)
            except ProductionRuntimePermissionError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(results)
        self.assertEqual(len(set(results)), 1)
        paths = list(self.permission_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    def test_expired_permission_derived_and_reissue_blocked(self) -> None:
        activation_id = self._open_window()
        issued = issue_production_runtime_permission(
            **self._perm_kwargs(activation_id, ttl_seconds=60)
        )
        self.assertEqual(issued.permission_state, PERMISSION_ISSUED)
        later = self._now + timedelta(seconds=61)
        expired = evaluate_production_runtime_permission(
            **self._perm_kwargs(activation_id, now=later)
        )
        self.assertEqual(expired.permission_state, PERMISSION_EXPIRED)
        self.assertIn(BLOCK_PERMISSION_EXPIRED, expired.blocking_items)
        self.assertEqual(
            expired.recommended_action,
            ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT,
        )
        with self.assertRaises(ProductionRuntimePermissionError) as ctx:
            issue_production_runtime_permission(
                **self._perm_kwargs(activation_id, now=later)
            )
        self.assertIn("permission_expired", str(ctx.exception))

    def test_permission_store_corruption_fail_closed(self) -> None:
        activation_id = self._open_window()
        path = self.permission_store_dir / f"{activation_id}.json"
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ProductionRuntimePermissionError):
            evaluate_production_runtime_permission(**self._perm_kwargs(activation_id))

    def test_safe_output_and_cli_parsers(self) -> None:
        activation_id = self._open_window()
        summary = issue_production_runtime_permission(**self._perm_kwargs(activation_id))
        output = format_production_runtime_permission_status(summary)
        self.assertIn("permission_state:", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertNotIn(_RUNTIME_EXECUTOR, output)
        self.assertNotIn(_PERMISSION_OPERATOR, output)
        self.assertNotIn(_CUTOVER_OPERATOR, output)
        self.assertNotIn(str(self.hermes_home), output)
        self.assertNotIn("/opt/data/", output)
        parser = build_coo_dispatch_parser()
        for cmd, handler in (
            (
                ["production", "governed-cutover", "permission", "status",
                 "--activation-request-id", activation_id],
                "_cmd_production_runtime_permission_status",
            ),
            (
                ["production", "governed-cutover", "permission", "check",
                 "--activation-request-id", activation_id,
                 "--executor-id", _RUNTIME_EXECUTOR],
                "_cmd_production_runtime_permission_check",
            ),
            (
                ["production", "governed-cutover", "permission", "issue",
                 "--activation-request-id", activation_id,
                 "--executor-id", _RUNTIME_EXECUTOR,
                 "--operator-id", _PERMISSION_OPERATOR,
                 "--ttl-seconds", "300"],
                "_cmd_production_runtime_permission_issue",
            ),
            (
                ["production", "governed-cutover", "permission", "show",
                 "--permission-id", summary.permission_id],
                "_cmd_production_runtime_permission_show",
            ),
            (
                ["production", "governed-cutover", "permission", "history",
                 "--activation-request-id", activation_id],
                "_cmd_production_runtime_permission_history",
            ),
        ):
            args = parser.parse_args(cmd)
            self.assertEqual(args.handler.__name__, handler)
        legacy = parser.parse_args(
            ["production", "cutover-check", "--ticket-id", "ticket-x"]
        )
        self.assertEqual(legacy.handler.__name__, "_cmd_production_cutover_check")

    def test_dashboard_and_release_summary(self) -> None:
        activation_id = self._open_window()
        issued = issue_production_runtime_permission(**self._perm_kwargs(activation_id))
        digest = resolve_latest_runtime_permission_dashboard_digest(
            governed_cutover_store_dir=self.governed_cutover_store_dir,
            window_store_dir=self.window_store_dir,
            permission_store_dir=self.permission_store_dir,
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
        self.assertEqual(digest.runtime_permission_state, PERMISSION_ISSUED)
        self.assertTrue(digest.runtime_permission_present)
        self.assertEqual(digest.runtime_permission_id, issued.permission_id)
        dashboard = build_operator_dashboard_summary(merged_config={})
        self.assertTrue(hasattr(dashboard, "runtime_permission_state"))
        release = build_production_runtime_permission_release_summary(issued)
        self.assertEqual(release.release_status, RELEASE_RUNTIME_PERMISSION_ISSUED)
        self.assertEqual(release.next_phase, "Phase_15D_governed_runtime_session")
        self.assertFalse(release.production_execution_allowed)
        self.assertFalse(release.cutover_started)
        self.assertFalse(release.runtime_invoked)
        self.assertEqual(issued.controlled_window_state, WINDOW_OPEN)
        contract = load_governed_cutover_contract(
            activation_id, store_dir=self.governed_cutover_store_dir
        )
        assert contract is not None
        self.assertFalse(contract.cutover_started)

    def test_no_subprocess_on_issue(self) -> None:
        activation_id = self._open_window()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                issue_production_runtime_permission(**self._perm_kwargs(activation_id))


if __name__ == "__main__":
    unittest.main()
