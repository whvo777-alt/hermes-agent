"""Tests for dispatch bundle persistence (Phase 10P)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import (
    DispatchExecutionBundle,
    build_dispatch_execution_bundle,
    get_by_unlock_token,
    mark_bundle_consumed,
    read_bundle,
    upsert_bundle_after_remint,
    upsert_bundle_preserving_identity,
    write_bundle,
)
from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionRequest,
    DispatchUnlockTokenStore,
    create_dispatch_unlock_token,
)
from agent.coo.tests.test_execution_dispatch_runtime import _approved_unlock_context


def _binding():
    ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
    token_store = DispatchUnlockTokenStore()
    token = create_dispatch_unlock_token(
        ticket,
        plan,
        dry_run,
        dry_run_request,
        execute_request,
        gate,
        requested_by=ticket.requester_id,
        token_store=token_store,
    )
    dispatch_request = DispatchExecutionRequest(
        dispatch_request_id="req-bundle-1",
        execute_request_id=token.execute_request_id,
        gate_id=gate.gate_id,
        ticket_id=ticket.ticket_id,
        plan_id=plan.plan_id,
        dry_run_run_id=token.dry_run_run_id,
        unlock_token_id=token.token_id,
        target_skills=list(token.target_skills),
        requested_by=ticket.requester_id,
        requested_at="2026-07-07T00:00:00+00:00",
    )
    return (
        ticket,
        plan,
        dry_run,
        dry_run_request,
        execute_request,
        gate,
        token,
        dispatch_request,
    )


class TestDispatchBundleStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.get_home_patch = patch(
            "agent.coo.dispatch_bundle_store.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.get_home_patch.start()

    def tearDown(self) -> None:
        self.get_home_patch.stop()
        self.tmp.cleanup()

    def _build_bundle(self, **overrides):
        (
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            token,
            dispatch_request,
        ) = _binding()
        bundle = build_dispatch_execution_bundle(
            ticket=ticket,
            plan=plan,
            dry_run=dry_run,
            dry_run_request=dry_run_request,
            execute_request=execute_request,
            gate=gate,
            token=token,
            dispatch_request=dispatch_request,
        )
        for key, value in overrides.items():
            bundle = DispatchExecutionBundle(
                **{**bundle.__dict__, key: value},
            )
        return bundle, ticket, token, dispatch_request

    def test_round_trip_write_read(self) -> None:
        bundle, ticket, token, dispatch_request = self._build_bundle()
        write_bundle(bundle, bundle_dir=self.bundle_dir)
        loaded = read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        self.assertEqual(loaded.bundle_id, bundle.bundle_id)
        self.assertEqual(loaded.unlock_token_id, token.token_id)
        self.assertEqual(loaded.dispatch_request_id, dispatch_request.dispatch_request_id)
        self.assertIn("ticket", loaded.snapshot)
        self.assertIn("unlock_token", loaded.snapshot)

    def test_get_by_unlock_token(self) -> None:
        bundle, ticket, token, _dispatch_request = self._build_bundle()
        write_bundle(bundle, bundle_dir=self.bundle_dir)
        found = get_by_unlock_token(token.token_id, bundle_dir=self.bundle_dir)
        assert found is not None
        self.assertEqual(found.ticket_id, ticket.ticket_id)

    def test_atomic_replace_preserves_bundle_id(self) -> None:
        (
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            token,
            dispatch_request,
        ) = _binding()
        bundle = build_dispatch_execution_bundle(
            ticket=ticket,
            plan=plan,
            dry_run=dry_run,
            dry_run_request=dry_run_request,
            execute_request=execute_request,
            gate=gate,
            token=token,
            dispatch_request=dispatch_request,
        )
        write_bundle(bundle, bundle_dir=self.bundle_dir)
        updated_request = DispatchExecutionRequest(
            dispatch_request_id="req-bundle-2",
            execute_request_id=dispatch_request.execute_request_id,
            gate_id=dispatch_request.gate_id,
            ticket_id=dispatch_request.ticket_id,
            plan_id=dispatch_request.plan_id,
            dry_run_run_id=dispatch_request.dry_run_run_id,
            unlock_token_id=token.token_id,
            target_skills=list(dispatch_request.target_skills),
            requested_by=dispatch_request.requested_by,
            requested_at="2026-07-07T00:01:00+00:00",
        )
        updated = build_dispatch_execution_bundle(
            ticket=ticket,
            plan=plan,
            dry_run=dry_run,
            dry_run_request=dry_run_request,
            execute_request=execute_request,
            gate=gate,
            token=token,
            dispatch_request=updated_request,
        )
        upsert_bundle_preserving_identity(updated, bundle_dir=self.bundle_dir)
        loaded = read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        self.assertEqual(loaded.bundle_id, bundle.bundle_id)
        self.assertEqual(loaded.dispatch_request_id, "req-bundle-2")

    def test_remint_updates_unlock_token(self) -> None:
        (
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            token,
            dispatch_request,
        ) = _binding()
        bundle = build_dispatch_execution_bundle(
            ticket=ticket,
            plan=plan,
            dry_run=dry_run,
            dry_run_request=dry_run_request,
            execute_request=execute_request,
            gate=gate,
            token=token,
            dispatch_request=dispatch_request,
        )
        write_bundle(bundle, bundle_dir=self.bundle_dir)

        token_store = DispatchUnlockTokenStore()
        token_store.save(token)
        token.superseded = True
        token_store.save(token)
        new_token = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )
        upsert_bundle_after_remint(
            ticket.ticket_id,
            new_token,
            ticket=ticket,
            plan=plan,
            dry_run=dry_run,
            dry_run_request=dry_run_request,
            execute_request=execute_request,
            gate=gate,
            bundle_dir=self.bundle_dir,
        )
        loaded = read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        self.assertEqual(loaded.unlock_token_id, new_token.token_id)
        self.assertTrue(loaded.snapshot.get("_remint_pending_prepare"))

    def test_consumed_bundle_rejected_on_read(self) -> None:
        bundle, ticket, _token, _dispatch_request = self._build_bundle()
        write_bundle(bundle, bundle_dir=self.bundle_dir)
        mark_bundle_consumed(ticket.ticket_id, bundle_dir=self.bundle_dir)
        with self.assertRaises(ValueError) as exc:
            read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        self.assertIn("consumed", str(exc.exception))

    def test_corrupted_json_rejected(self) -> None:
        bundle, ticket, _token, _dispatch_request = self._build_bundle()
        write_bundle(bundle, bundle_dir=self.bundle_dir)
        path = self.bundle_dir / f"{ticket.ticket_id}.json"
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        self.assertIn("corrupted", str(exc.exception))

    def test_id_mismatch_rejected(self) -> None:
        bundle, ticket, _token, _dispatch_request = self._build_bundle()
        payload = bundle.to_dict()
        payload["unlock_token_id"] = "wrong-token"
        self.bundle_dir.mkdir(parents=True, exist_ok=True)
        path = self.bundle_dir / f"{ticket.ticket_id}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        self.assertIn("mismatch", str(exc.exception))

    def test_repository2_like_path_rejected_without_mkdir(self) -> None:
        bundle, ticket, _token, _dispatch_request = self._build_bundle()
        outside = Path("/opt/data/multi-content-pipeline/fake-bundles")
        with self.assertRaises(ValueError):
            write_bundle(bundle, bundle_dir=outside)
        self.assertFalse(outside.exists())

    def test_outside_hermes_path_rejected_without_mkdir(self) -> None:
        bundle, _ticket, _token, _dispatch_request = self._build_bundle()
        outside = Path(self.tmp.name) / "outside-hermes" / "bundles"
        with self.assertRaises(ValueError):
            write_bundle(bundle, bundle_dir=outside)
        self.assertFalse(outside.exists())

    def test_symlink_escape_rejected(self) -> None:
        bundle, ticket, _token, _dispatch_request = self._build_bundle()
        escape_target = Path(self.tmp.name) / "escape-target"
        escape_target.mkdir()
        link_dir = self.hermes_home / "coo" / "escape-link"
        link_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(escape_target, link_dir)
        with self.assertRaises(ValueError):
            write_bundle(bundle, bundle_dir=link_dir / "bundles")
        self.assertFalse((escape_target / f"{ticket.ticket_id}.json").exists())

    def test_subprocess_not_used(self) -> None:
        bundle, ticket, _token, _dispatch_request = self._build_bundle()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")):
                write_bundle(bundle, bundle_dir=self.bundle_dir)
                loaded = read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        self.assertEqual(loaded.ticket_id, ticket.ticket_id)


if __name__ == "__main__":
    unittest.main()
