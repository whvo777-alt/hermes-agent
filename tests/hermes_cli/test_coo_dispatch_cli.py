"""CLI skeleton tests for hermes coo dispatch (Phase 10L)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from hermes_cli import coo_dispatch
from hermes_cli.coo_dispatch import (
    PRODUCTION_ROOT_HARD_DENY,
    assert_pipeline_root_allowed_for_cli,
    build_coo_dispatch_parser,
    main,
)


class TestCooDispatchCli(unittest.TestCase):
    def test_confirm_run_parser_accepts_required_args(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "confirm-run",
                "--ticket-id",
                "ticket-1",
                "--plan-id",
                "plan-1",
                "--unlock-token-id",
                "token-1",
                "--dispatch-request-id",
                "req-1",
                "--operator-id",
                "op-1",
                "--operator-name",
                "Operator",
                "--reason",
                "staging validation",
                "--phrase",
                "CONFIRM-REPOSITORY2-EXECUTION",
            ]
        )
        self.assertEqual(args.coo_dispatch_command, "confirm-run")
        self.assertEqual(args.ticket_id, "ticket-1")
        self.assertEqual(args.phrase, "CONFIRM-REPOSITORY2-EXECUTION")

    def test_confirm_run_parser_requires_phrase(self) -> None:
        parser = build_coo_dispatch_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "confirm-run",
                    "--ticket-id",
                    "ticket-1",
                    "--plan-id",
                    "plan-1",
                    "--unlock-token-id",
                    "token-1",
                    "--dispatch-request-id",
                    "req-1",
                    "--operator-id",
                    "op-1",
                    "--operator-name",
                    "Operator",
                    "--reason",
                    "staging validation",
                ]
            )


    def test_run_subcommand_parser_accepts_required_args(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "run",
                "--ticket-id",
                "ticket-1",
                "--unlock-token-id",
                "token-1",
                "--confirmation-id",
                "confirm-1",
                "--requester-id",
                "requester-1",
                "--pipeline-root",
                "/tmp/fake-pipeline",
            ]
        )
        self.assertEqual(args.coo_dispatch_command, "run")
        self.assertEqual(args.ticket_id, "ticket-1")
        self.assertEqual(args.unlock_token_id, "token-1")
        self.assertFalse(args.dry_run)

    def test_production_root_hard_deny_helper(self) -> None:
        self.assertIn("/opt/data/multi-content-pipeline", PRODUCTION_ROOT_HARD_DENY)
        with self.assertRaises(ValueError) as exc:
            assert_pipeline_root_allowed_for_cli("/opt/data/multi-content-pipeline")
        self.assertIn("hard-denied", str(exc.exception))

    def test_production_root_outputs_subdirectory_denied(self) -> None:
        with self.assertRaises(ValueError):
            assert_pipeline_root_allowed_for_cli(
                "/opt/data/multi-content-pipeline/outputs"
            )

    def test_production_root_outputs_audit_subdirectory_denied(self) -> None:
        with self.assertRaises(ValueError):
            assert_pipeline_root_allowed_for_cli(
                "/opt/data/multi-content-pipeline/outputs/audit"
            )

    def test_production_root_internal_symlink_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            denied_root = os.path.join(tmp, "denied-root")
            internal_dir = os.path.join(denied_root, "outputs")
            os.makedirs(internal_dir)
            link_path = os.path.join(tmp, "escape-link")
            os.symlink(internal_dir, link_path)
            with patch.object(
                coo_dispatch,
                "PRODUCTION_ROOT_HARD_DENY",
                (denied_root,),
            ):
                with self.assertRaises(ValueError):
                    assert_pipeline_root_allowed_for_cli(link_path)

    def test_external_pipeline_root_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            external_root = os.path.join(tmp, "fake-pipeline")
            os.makedirs(external_root)
            assert_pipeline_root_allowed_for_cli(external_root)

    def test_confirm_run_persists_confirmation_without_phrase_in_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            confirmation_dir = hermes_home / "coo" / "confirmations"
            with patch(
                "agent.coo.production_executor_confirmation.get_hermes_home",
                return_value=hermes_home,
            ):
                stdout = io.StringIO()
                with patch.object(sys, "stdout", stdout):
                    exit_code = main(
                        [
                            "confirm-run",
                            "--ticket-id",
                            "ticket-cli-1",
                            "--plan-id",
                            "plan-cli-1",
                            "--unlock-token-id",
                            "token-cli-1",
                            "--dispatch-request-id",
                            "req-cli-1",
                            "--operator-id",
                            "op-cli",
                            "--operator-name",
                            "CLI Operator",
                            "--reason",
                            "cli persistence test",
                            "--phrase",
                            "CONFIRM-REPOSITORY2-EXECUTION",
                        ]
                    )
            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("confirmation_id:", output)
            self.assertNotIn("CONFIRM-REPOSITORY2-EXECUTION", output)
            confirmation_files = list(confirmation_dir.glob("*.json"))
            self.assertEqual(len(confirmation_files), 1)
            payload = json.loads(confirmation_files[0].read_text(encoding="utf-8"))
            self.assertNotIn("confirmation_phrase", payload)
            self.assertTrue(payload["phrase_verified"])
            self.assertEqual(payload["operator_id"], "op-cli")

    def test_confirm_run_missing_phrase_fails(self) -> None:
        parser = build_coo_dispatch_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "confirm-run",
                    "--ticket-id",
                    "ticket-1",
                    "--plan-id",
                    "plan-1",
                    "--unlock-token-id",
                    "token-1",
                    "--dispatch-request-id",
                    "req-1",
                    "--operator-id",
                    "op-1",
                    "--operator-name",
                    "Operator",
                    "--reason",
                    "staging validation",
                ]
            )

    def test_run_command_fail_closed_without_runner(self) -> None:
        fixture_tmp = tempfile.TemporaryDirectory()
        hermes_home = Path(fixture_tmp.name) / ".hermes"
        hermes_home.mkdir()
        pipeline_root = Path(fixture_tmp.name) / "fake-pipeline"
        pipeline_root.mkdir()
        bundle_dir = hermes_home / "coo" / "dispatch-bundles"
        confirmation_dir = hermes_home / "coo" / "confirmations"
        with (
            patch(
                "agent.coo.dispatch_bundle_store.get_hermes_home",
                return_value=hermes_home,
            ),
            patch(
                "agent.coo.production_executor_confirmation.get_hermes_home",
                return_value=hermes_home,
            ),
            patch(
                "agent.coo.dispatch_cli_run.get_hermes_home",
                return_value=hermes_home,
            ),
            patch(
                "agent.coo.production_executor_factory.get_hermes_home",
                return_value=hermes_home,
            ),
            patch(
                "agent.coo.dispatch_execution_audit.get_hermes_home",
                return_value=hermes_home,
            ),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
        ):
            from agent.coo.gateway_execution_dispatch import prepare_dispatch_for_gateway_ticket
            from agent.coo.production_executor_confirmation import (
                REQUIRED_CONFIRMATION_PHRASE,
                create_production_executor_confirmation,
            )
            from agent.coo.tests.test_gateway_execution_dispatch import (
                _seed_approved_dispatch_pipeline,
            )

            ctx = _seed_approved_dispatch_pipeline()
            ticket = ctx["ticket"]
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
                bundle_dir=bundle_dir,
            )
            confirmation = create_production_executor_confirmation(
                ticket_id=ticket.ticket_id,
                plan_id=prepare["unlock_token"]["plan_id"],
                unlock_token_id=prepare["unlock_token"]["token_id"],
                dispatch_request_id=prepare["dispatch_request"]["dispatch_request_id"],
                operator_id="op-cli",
                operator_name="CLI Operator",
                confirmation_reason="cli run test",
                confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                persist_to_file=True,
                confirmation_dir=confirmation_dir,
            )
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr):
                exit_code = main(
                    [
                        "run",
                        "--ticket-id",
                        ticket.ticket_id,
                        "--unlock-token-id",
                        prepare["unlock_token"]["token_id"],
                        "--confirmation-id",
                        confirmation.confirmation_id,
                        "--requester-id",
                        ticket.requester_id,
                        "--pipeline-root",
                        str(pipeline_root),
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("production runner is not configured", stderr.getvalue())
        fixture_tmp.cleanup()

    def test_subprocess_not_used_by_confirm_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            with patch(
                "agent.coo.production_executor_confirmation.get_hermes_home",
                return_value=hermes_home,
            ):
                with patch.object(
                    subprocess,
                    "run",
                    side_effect=AssertionError("no subprocess"),
                ):
                    with patch.object(
                        subprocess,
                        "Popen",
                        side_effect=AssertionError("no subprocess"),
                    ):
                        exit_code = main(
                            [
                                "confirm-run",
                                "--ticket-id",
                                "ticket-1",
                                "--plan-id",
                                "plan-1",
                                "--unlock-token-id",
                                "token-1",
                                "--dispatch-request-id",
                                "req-1",
                                "--operator-id",
                                "op-1",
                                "--operator-name",
                                "Operator",
                                "--reason",
                                "test",
                                "--phrase",
                                "CONFIRM-REPOSITORY2-EXECUTION",
                            ]
                        )
            self.assertEqual(exit_code, 0)


class TestCooDispatchMainEntrypoint(unittest.TestCase):
    """Phase 10R — hermes coo dispatch registered in main.py."""

    _CONFIRM_RUN_ARGS = [
        "confirm-run",
        "--ticket-id",
        "ticket-1",
        "--plan-id",
        "plan-1",
        "--unlock-token-id",
        "token-1",
        "--dispatch-request-id",
        "req-1",
        "--operator-id",
        "op-1",
        "--operator-name",
        "Operator",
        "--reason",
        "test",
        "--phrase",
        "CONFIRM-REPOSITORY2-EXECUTION",
    ]

    @contextmanager
    def _main_patches(self):
        import hermes_cli.main as hermes_main

        with (
            patch.object(hermes_main, "_try_termux_fast_tui_launch", return_value=False),
            patch.object(hermes_main, "_try_termux_fast_cli_launch", return_value=False),
            patch.object(hermes_main, "_plugin_cli_discovery_needed", return_value=False),
            patch.object(hermes_main, "_prepare_agent_startup"),
        ):
            yield

    def test_main_entrypoint_dispatch_help(self) -> None:
        import hermes_cli.main as hermes_main

        argv_backup = sys.argv[:]
        sys.argv = ["hermes", "coo", "dispatch", "--help"]
        buf = io.StringIO()
        try:
            with self._main_patches():
                with patch.object(sys, "stdout", buf):
                    with self.assertRaises(SystemExit) as ctx:
                        hermes_main.main()
            self.assertEqual(ctx.exception.code, 0)
        finally:
            sys.argv = argv_backup
        output = buf.getvalue()
        self.assertIn("confirm-run", output)
        self.assertIn("run", output)

    def test_main_entrypoint_confirm_run_parser(self) -> None:
        import hermes_cli.main as hermes_main

        captured: dict[str, str] = {}

        def fake_confirm_run(args) -> int:
            captured["coo_dispatch_command"] = args.coo_dispatch_command
            captured["ticket_id"] = args.ticket_id
            return 0

        argv_backup = sys.argv[:]
        sys.argv = ["hermes", "coo", "dispatch", *self._CONFIRM_RUN_ARGS]
        try:
            with self._main_patches():
                with patch("hermes_cli.coo_dispatch._cmd_confirm_run", fake_confirm_run):
                    hermes_main.main()
        finally:
            sys.argv = argv_backup
        self.assertEqual(captured["coo_dispatch_command"], "confirm-run")
        self.assertEqual(captured["ticket_id"], "ticket-1")

    def test_main_entrypoint_run_dry_run_parser(self) -> None:
        import hermes_cli.main as hermes_main

        captured: dict[str, object] = {}

        def fake_run(args) -> int:
            captured["coo_dispatch_command"] = args.coo_dispatch_command
            captured["dry_run"] = bool(args.dry_run)
            return 0

        argv_backup = sys.argv[:]
        sys.argv = [
            "hermes",
            "coo",
            "dispatch",
            "run",
            "--ticket-id",
            "ticket-1",
            "--unlock-token-id",
            "token-1",
            "--confirmation-id",
            "confirm-1",
            "--requester-id",
            "requester-1",
            "--pipeline-root",
            "/tmp/fake-pipeline",
            "--dry-run",
        ]
        try:
            with self._main_patches():
                with patch("hermes_cli.coo_dispatch._cmd_run", fake_run):
                    hermes_main.main()
        finally:
            sys.argv = argv_backup
        self.assertEqual(captured["coo_dispatch_command"], "run")
        self.assertTrue(captured["dry_run"])

    def test_main_entrypoint_run_fail_closed_without_runner(self) -> None:
        import hermes_cli.main as hermes_main
        from agent.coo.gateway_execution_dispatch import prepare_dispatch_for_gateway_ticket
        from agent.coo.production_executor_confirmation import (
            REQUIRED_CONFIRMATION_PHRASE,
            create_production_executor_confirmation,
        )
        from agent.coo.tests.test_gateway_execution_dispatch import _seed_approved_dispatch_pipeline

        fixture_tmp = tempfile.TemporaryDirectory()
        hermes_home = Path(fixture_tmp.name) / ".hermes"
        hermes_home.mkdir()
        pipeline_root = Path(fixture_tmp.name) / "fake-pipeline"
        pipeline_root.mkdir()
        bundle_dir = hermes_home / "coo" / "dispatch-bundles"
        confirmation_dir = hermes_home / "coo" / "confirmations"
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        with (
            patch(
                "agent.coo.dispatch_bundle_store.get_hermes_home",
                return_value=hermes_home,
            ),
            patch(
                "agent.coo.production_executor_confirmation.get_hermes_home",
                return_value=hermes_home,
            ),
            patch(
                "agent.coo.dispatch_cli_run.get_hermes_home",
                return_value=hermes_home,
            ),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
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
                bundle_dir=bundle_dir,
            )
            confirmation = create_production_executor_confirmation(
                ticket_id=ticket.ticket_id,
                plan_id=prepare["unlock_token"]["plan_id"],
                unlock_token_id=prepare["unlock_token"]["token_id"],
                dispatch_request_id=prepare["dispatch_request"]["dispatch_request_id"],
                operator_id="op-cli",
                operator_name="CLI Operator",
                confirmation_reason="cli run test",
                confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                persist_to_file=True,
                confirmation_dir=confirmation_dir,
            )
            argv_backup = sys.argv[:]
            sys.argv = [
                "hermes",
                "coo",
                "dispatch",
                "run",
                "--ticket-id",
                ticket.ticket_id,
                "--unlock-token-id",
                prepare["unlock_token"]["token_id"],
                "--confirmation-id",
                confirmation.confirmation_id,
                "--requester-id",
                ticket.requester_id,
                "--pipeline-root",
                str(pipeline_root),
            ]
            stderr = io.StringIO()
            try:
                with self._main_patches():
                    with patch.object(sys, "stderr", stderr):
                        hermes_main.main()
            finally:
                sys.argv = argv_backup
            self.assertIn("production runner is not configured", stderr.getvalue())
        fixture_tmp.cleanup()

    def test_main_entrypoint_avoids_repository2_paths(self) -> None:
        import hermes_cli.main as hermes_main

        for denied in PRODUCTION_ROOT_HARD_DENY:
            argv_backup = sys.argv[:]
            sys.argv = [
                "hermes",
                "coo",
                "dispatch",
                "run",
                "--ticket-id",
                "ticket-1",
                "--unlock-token-id",
                "token-1",
                "--confirmation-id",
                "confirm-1",
                "--requester-id",
                "requester-1",
                "--pipeline-root",
                denied,
                "--dry-run",
            ]
            stderr = io.StringIO()
            try:
                with self._main_patches():
                    with patch.object(sys, "stderr", stderr):
                        hermes_main.main()
            finally:
                sys.argv = argv_backup
            self.assertIn("hard-denied", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
