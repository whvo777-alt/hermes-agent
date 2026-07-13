"""Phase 13P tests — operator documentation and CLI help integrity."""

from __future__ import annotations

import io
import re
import sys
import unittest
from pathlib import Path

from hermes_cli.coo_dispatch import build_coo_dispatch_parser

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPERATOR_DOCS = _REPO_ROOT / "docs" / "operator"
_README = _REPO_ROOT / "README.md"

_EXPECTED_OPERATOR_DOCS = (
    "README.md",
    "Dispatch_Runbook.md",
    "Gateway_Runbook.md",
    "Recovery_Runbook.md",
    "Pilot_Runbook.md",
    "Production_Signoff.md",
    "Operator_Checklist.md",
    "CLI_Command_Reference.md",
    "Architecture_Overview.md",
    "Recommended_Action_Mapping.md",
)

_FORBIDDEN_DOC_TOKENS = (
    "unlock_token_id",
    "confirmation_phrase",
    "pipeline_root:",
    "/opt/data/",
    "pipeline.js",
    "argv",
    "stderr",
    "stdout",
    "HERMES_",
)


class TestOperatorDocumentation(unittest.TestCase):
    def test_operator_doc_files_exist(self) -> None:
        self.assertTrue(_OPERATOR_DOCS.is_dir())
        for name in _EXPECTED_OPERATOR_DOCS:
            path = _OPERATOR_DOCS / name
            self.assertTrue(path.is_file(), f"missing operator doc: {name}")

    def test_operator_index_links_resolve(self) -> None:
        index_text = (_OPERATOR_DOCS / "README.md").read_text(encoding="utf-8")
        links = re.findall(r"\]\(([^)]+\.md)\)", index_text)
        self.assertGreater(len(links), 0)
        for link in links:
            target = _OPERATOR_DOCS / link
            self.assertTrue(target.is_file(), f"broken index link: {link}")

    def test_readme_links_to_operator_docs(self) -> None:
        readme_text = _README.read_text(encoding="utf-8")
        self.assertIn("docs/operator/README.md", readme_text)
        self.assertIn("Dispatch Runbook", readme_text)
        self.assertIn("Gateway Runbook", readme_text)
        self.assertIn("COO Operations Quick Reference", readme_text)

    def test_operator_docs_avoid_forbidden_tokens(self) -> None:
        for name in _EXPECTED_OPERATOR_DOCS:
            text = (_OPERATOR_DOCS / name).read_text(encoding="utf-8").lower()
            for token in _FORBIDDEN_DOC_TOKENS:
                self.assertNotIn(
                    token.lower(),
                    text,
                    f"{name} contains forbidden token {token!r}",
                )

    def test_dispatch_parser_help_lists_gateway_commands(self) -> None:
        parser = build_coo_dispatch_parser()
        buf = io.StringIO()
        with patch_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                parser.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        output = buf.getvalue()
        self.assertIn("gateway", output)
        self.assertIn("dashboard", output)
        self.assertIn("correlation", output)
        self.assertIn("docs/operator/README.md", output)
        self.assertIn("Production execution remains disabled", output)
        self.assertIn("Read-only command", output)
        self.assertIn("operator guidance", output)

    def test_gateway_dashboard_subcommand_parses(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            ["gateway", "dashboard", "--ticket-id", "ticket-1", "--limit", "5"]
        )
        self.assertEqual(args.coo_dispatch_command, "gateway")
        self.assertEqual(args.coo_dispatch_gateway_command, "dashboard")
        self.assertEqual(args.ticket_id, "ticket-1")
        self.assertEqual(args.limit, 5)

    def test_gateway_correlation_diff_subcommand_parses(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "gateway",
                "correlation",
                "diff",
                "--left-gateway-request-id",
                "gw-left",
                "--right-gateway-request-id",
                "gw-right",
            ]
        )
        self.assertEqual(args.coo_dispatch_gateway_correlation_command, "diff")
        self.assertEqual(args.left_gateway_request_id, "gw-left")
        self.assertEqual(args.right_gateway_request_id, "gw-right")

    def test_main_entrypoint_dispatch_help_includes_gateway(self) -> None:
        import hermes_cli.main as hermes_main
        from tests.hermes_cli.test_coo_dispatch_cli import TestCooDispatchMainEntrypoint

        argv_backup = sys.argv[:]
        sys.argv = ["hermes", "coo", "dispatch", "--help"]
        buf = io.StringIO()
        try:
            with TestCooDispatchMainEntrypoint()._main_patches():
                with patch_stdout(buf):
                    with self.assertRaises(SystemExit) as ctx:
                        hermes_main.main()
            self.assertEqual(ctx.exception.code, 0)
        finally:
            sys.argv = argv_backup
        output = buf.getvalue()
        self.assertIn("gateway", output)
        self.assertIn("pilot", output)
        self.assertIn("production", output)


def patch_stdout(buffer: io.StringIO):
    from contextlib import contextmanager
    from unittest.mock import patch

    @contextmanager
    def _cm():
        with patch.object(sys, "stdout", buffer):
            yield

    return _cm()


if __name__ == "__main__":
    unittest.main()
