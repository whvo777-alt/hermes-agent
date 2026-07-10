"""Phase 10U tests — CLI dispatch policy preflight (dry-run only)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import read_bundle
from agent.coo.dispatch_cli_preflight import (
    format_dispatch_preflight_summary,
    run_dispatch_policy_preflight,
)
from agent.coo.dispatch_cli_run import execute_coo_dispatch_run
from agent.coo.dispatch_executor_config import (
    load_dispatch_executor_policy,
    parse_dispatch_executor_config,
)
from agent.coo.gateway_execution_dispatch import prepare_dispatch_for_gateway_ticket
from agent.coo.production_executor_confirmation import (
    REQUIRED_CONFIRMATION_PHRASE,
    create_production_executor_confirmation,
    read_confirmation,
)
from agent.coo.tests.test_gateway_execution_dispatch import _seed_approved_dispatch_pipeline


def _enabled_executor_config(pipeline_root: Path) -> dict:
    return {
        "coo": {
            "dispatch": {
                "executor": {
                    "enabled": True,
                    "allowed_pipeline_roots": [str(pipeline_root)],
                }
            }
        }
    }


class _PreflightFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.pipeline_root = Path(self.tmp.name) / "fake-pipeline"
        self.pipeline_root.mkdir()
        self._patches = [
            patch(
                "agent.coo.dispatch_bundle_store.get_hermes_home",
                return_value=self.hermes_home,
            ),
            patch(
                "agent.coo.production_executor_confirmation.get_hermes_home",
                return_value=self.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_cli_run.get_hermes_home",
                return_value=self.hermes_home,
            ),
        ]

    def start(self) -> None:
        for item in self._patches:
            item.start()

    def stop(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self.tmp.cleanup()

    def seed(self) -> dict:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            prepare = prepare_dispatch_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                run_store=ctx["run_store"],
                dry_run_request_store=ctx["dry_run_request_store"],
                execute_request_store=ctx["execute_request_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                bundle_dir=self.bundle_dir,
            )
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=prepare["unlock_token"]["plan_id"],
            unlock_token_id=prepare["unlock_token"]["token_id"],
            dispatch_request_id=prepare["dispatch_request"]["dispatch_request_id"],
            operator_id="op-preflight",
            operator_name="Preflight Operator",
            confirmation_reason="preflight test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            persist_to_file=True,
            confirmation_dir=self.confirmation_dir,
        )
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        return {
            "ticket": ticket,
            "prepare": prepare,
            "confirmation": confirmation,
            "bundle": bundle,
        }


class TestDispatchCliPreflight(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _PreflightFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed()

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_enabled_false_fail_closed(self) -> None:
        summary = run_dispatch_policy_preflight(
            bundle=self.seeded["bundle"],
            confirmation=self.seeded["confirmation"],
            pipeline_root=str(self.fixture.pipeline_root),
            merged_config={"coo": {"dispatch": {"executor": {"enabled": False}}}},
        )
        self.assertFalse(summary.all_passed)
        self.assertIn("policy_enabled", summary.failed_check_names)

    def test_valid_isolated_allowlist_passes(self) -> None:
        summary = run_dispatch_policy_preflight(
            bundle=self.seeded["bundle"],
            confirmation=self.seeded["confirmation"],
            pipeline_root=str(self.fixture.pipeline_root),
            merged_config=_enabled_executor_config(self.fixture.pipeline_root),
        )
        self.assertTrue(summary.all_passed)
        self.assertEqual(summary.failed_check_names, ())

    def test_production_root_in_config_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            parse_dispatch_executor_config(
                {
                    "enabled": False,
                    "allowed_pipeline_roots": ["/opt/data/multi-content-pipeline"],
                }
            )
        self.assertIn("production Repository2", str(exc.exception))

    def test_preflight_pass_does_not_consume(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.build_pipeline_dispatch_executor",
                side_effect=AssertionError("no factory"),
            ),
            patch(
                "agent.coo.dispatch_cli_run.run_approved_dispatch",
                side_effect=AssertionError("no runner"),
            ),
            patch(
                "agent.coo.dispatch_cli_run.mark_bundle_consumed",
                side_effect=AssertionError("no consume"),
            ),
            patch(
                "agent.coo.dispatch_cli_run.mark_confirmation_consumed_file",
                side_effect=AssertionError("no consume"),
            ),
        ):
            result = execute_coo_dispatch_run(
                ticket_id=ticket.ticket_id,
                confirmation_id=confirmation.confirmation_id,
                unlock_token_id=prepare["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                pipeline_root=str(self.fixture.pipeline_root),
                dry_run=True,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                merged_config=_enabled_executor_config(self.fixture.pipeline_root),
            )
        self.assertTrue(result.preflight is not None and result.preflight.all_passed)
        self.assertFalse(result.consumed)
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        self.assertEqual(bundle.consumed_at, "")
        loaded = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        self.assertFalse(loaded.consumed)

    def test_preflight_fail_does_not_consume(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        result = execute_coo_dispatch_run(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            dry_run=True,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            merged_config={"coo": {"dispatch": {"executor": {"enabled": False}}}},
        )
        self.assertFalse(result.preflight is not None and result.preflight.all_passed)
        self.assertFalse(result.consumed)
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        self.assertEqual(bundle.consumed_at, "")
        loaded = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        self.assertFalse(loaded.consumed)

    def test_remint_pending_rejected_before_preflight(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        bundle_path = self.fixture.bundle_dir / f"{ticket.ticket_id}.json"
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        payload["snapshot"]["_remint_pending_prepare"] = True
        bundle_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            execute_coo_dispatch_run(
                ticket_id=ticket.ticket_id,
                confirmation_id=confirmation.confirmation_id,
                unlock_token_id=prepare["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                pipeline_root=str(self.fixture.pipeline_root),
                dry_run=True,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                merged_config=_enabled_executor_config(self.fixture.pipeline_root),
            )
        self.assertIn("pending prepare", str(exc.exception))

    def test_summary_excludes_secrets_paths_and_snapshot(self) -> None:
        summary = run_dispatch_policy_preflight(
            bundle=self.seeded["bundle"],
            confirmation=self.seeded["confirmation"],
            pipeline_root=str(self.fixture.pipeline_root),
            merged_config={"coo": {"dispatch": {"executor": {"enabled": False}}}},
        )
        rendered = format_dispatch_preflight_summary(summary)
        self.assertIn("preflight: failed", rendered)
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE, rendered)
        self.assertNotIn(self.seeded["prepare"]["unlock_token"]["token_id"], rendered)
        self.assertNotIn(str(self.fixture.pipeline_root), rendered)
        self.assertNotIn("/opt/data/multi-content-pipeline", rendered)
        self.assertNotIn(json.dumps(self.seeded["bundle"].snapshot), rendered)
        for secret_key in ("API_KEY", "PASSWORD", "SECRET", "TOKEN="):
            self.assertNotIn(secret_key, rendered)

    def test_load_dispatch_executor_policy_from_merged_config(self) -> None:
        policy = load_dispatch_executor_policy(
            _enabled_executor_config(self.fixture.pipeline_root),
        )
        self.assertTrue(policy.enabled)
        self.assertEqual(
            policy.allowed_pipeline_roots,
            (str(self.fixture.pipeline_root),),
        )
