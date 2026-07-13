"""Phase 13Q tests — operator guidance, CLI help polish, and doc integration."""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_operator_guidance import run_operator_guidance_show
from agent.coo.dispatch_gateway_correlation_explorer import (
    CooDispatchGatewayCorrelationChain,
    format_gateway_correlation_chain,
)
from agent.coo.dispatch_gateway_discord_status import (
    DiscordGatewayStatusResult,
    format_discord_gateway_status_response,
)
from agent.coo.dispatch_gateway_operator_dashboard import (
    CooDispatchGatewayCorrelationDiff,
    CooDispatchGatewayOperatorDashboardSummary,
    format_gateway_correlation_diff,
    format_operator_dashboard_summary,
)
from agent.coo.dispatch_operator_guidance import (
    KNOWN_RECOMMENDED_ACTIONS,
    RUNBOOK_SECTION_ANCHORS,
    OperatorGuidanceError,
    resolve_operator_guidance,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser, _cmd_operator_guidance
from hermes_cli.coo_dispatch_help_text import (
    HELP_ISOLATED_MOCK,
    HELP_PRODUCTION_BLOCKED,
    HELP_READ_ONLY,
    HELP_REPOSITORY_HARD_DENY,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPERATOR_DOCS = _REPO_ROOT / "docs" / "operator"
_README = _REPO_ROOT / "README.md"

_FORBIDDEN_GUIDANCE_TOKENS = (
    "pipeline_root",
    "/opt/data/",
    "pipeline.js",
    "unlock_token",
    "confirmation_phrase",
    "argv",
    "cwd",
    "stdout",
    "stderr",
    "operator_reason",
    "http://",
    "https://",
)

_REQUIRED_CORE_ACTIONS = (
    "no_action_required",
    "run_gateway_pilot_dry_run",
    "run_gateway_mock_pilot",
    "collect_more_history",
    "inspect_latest_failure",
    "inspect_missing_evidence",
    "resolve_recovery_required",
    "resolve_correlation_mismatch",
    "resolve_regression_failure",
    "stage_gateway",
    "maintain_production_block",
)


def _help_output(argv: list[str]) -> str:
    parser = build_coo_dispatch_parser()
    buf = io.StringIO()
    with patch.object(sys, "stdout", buf):
        with unittest.TestCase().assertRaises(SystemExit) as ctx:
            parser.parse_args([*argv, "--help"])
    if ctx.exception.code != 0:
        raise AssertionError(f"help exit {ctx.exception.code} for {argv}")
    return buf.getvalue()


def _hermes_digest(root: Path) -> str:
    if not root.exists():
        return ""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            parts.append(f"{rel}:{digest}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _dashboard_summary(**overrides: object) -> CooDispatchGatewayOperatorDashboardSummary:
    base = {
        "dashboard_health": "HEALTHY",
        "gateway_state": "staged",
        "readiness_level": "ready",
        "signoff_ready": True,
        "cutover_ready": True,
        "regression_status": "PASS",
        "trend_status": "STABLE",
        "latest_gateway_request_id": "gw-1",
        "latest_pilot_attempt_id": "pilot-1",
        "latest_execution_attempt_id": "exec-1",
        "latest_dispatch_run_id": "run-1",
        "latest_request_status": "completed",
        "evidence_present": True,
        "audit_present": True,
        "consume_state": "committed",
        "consumed": True,
        "recovery_required": False,
        "repair_lock_held": False,
        "correlation_valid": True,
        "chain_complete": True,
        "consecutive_failures": 0,
        "total_recent_requests": 1,
        "failed_recent_requests": 0,
        "recommended_action": "no_action_required",
        "production_execution_allowed": False,
        "production_root_hard_deny": True,
        "gateway_execution_scope": "isolated_gateway_mock",
        "facade_connected": True,
    }
    base.update(overrides)
    return CooDispatchGatewayOperatorDashboardSummary(**base)


def _correlation_chain(**overrides: object) -> CooDispatchGatewayCorrelationChain:
    base = {
        "query_type": "gateway_request",
        "query_id": "gw-1",
        "gateway_request_id": "gw-1",
        "ticket_id": "ticket-1",
        "confirmation_id": "conf-1",
        "session_id": "sess-1",
        "pilot_attempt_id": "pilot-1",
        "execution_attempt_id": "exec-1",
        "dispatch_run_id": "run-1",
        "request_status": "completed",
        "pilot_status": "success",
        "execution_status": "success",
        "audit_present": True,
        "evidence_present": True,
        "consume_state": "committed",
        "consumed": True,
        "recovery_required": False,
        "repair_attempt_id": "(none)",
        "repair_audit_present": False,
        "repair_lock_held": False,
        "correlation_valid": True,
        "chain_complete": True,
        "ambiguity_detected": False,
        "failure_reason_code": "(none)",
        "recommended_action": "resolve_recovery_required",
        "production_execution_allowed": False,
        "production_root_hard_deny": True,
        "gateway_execution_scope": "isolated_gateway_mock",
    }
    base.update(overrides)
    return CooDispatchGatewayCorrelationChain(**base)


class TestOperatorGuidanceMapping(unittest.TestCase):
    def test_all_known_actions_resolve(self) -> None:
        for action in sorted(KNOWN_RECOMMENDED_ACTIONS):
            guidance = resolve_operator_guidance(action)
            self.assertEqual(guidance.recommended_action, action)
            self.assertFalse(guidance.production_execution_allowed)
            self.assertIn("#", f"{guidance.runbook_name}#{guidance.runbook_section}")

    def test_required_core_actions_present(self) -> None:
        for action in _REQUIRED_CORE_ACTIONS:
            self.assertIn(action, KNOWN_RECOMMENDED_ACTIONS)

    def test_unknown_action_fail_closed(self) -> None:
        with self.assertRaises(OperatorGuidanceError):
            resolve_operator_guidance("not_a_real_action")

    def test_path_separator_in_action_rejected(self) -> None:
        with self.assertRaises(OperatorGuidanceError):
            resolve_operator_guidance("foo/bar")


class TestOperatorGuidanceCli(unittest.TestCase):
    def test_guidance_cli_safe_output(self) -> None:
        output, exit_code = run_operator_guidance_show("resolve_recovery_required")
        self.assertEqual(exit_code, 0)
        self.assertIn("recommended_action: resolve_recovery_required", output)
        self.assertIn("runbook_ref: Recovery_Runbook#manual-recovery", output)
        self.assertIn("guidance_summary:", output)
        self.assertIn("production_execution_allowed: false", output)
        lowered = output.lower()
        for token in _FORBIDDEN_GUIDANCE_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_guidance_cli_unknown_exit_one(self) -> None:
        args = type("Args", (), {"recommended_action": "bogus_action"})()
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            exit_code = _cmd_operator_guidance(args)
        self.assertEqual(exit_code, 1)
        self.assertIn("Unknown recommended_action", stderr.getvalue())

    def test_guidance_subcommand_parses(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            ["operator", "guidance", "--recommended-action", "stage_gateway"]
        )
        self.assertEqual(args.coo_dispatch_operator_command, "guidance")
        self.assertEqual(args.recommended_action, "stage_gateway")

    def test_guidance_no_subprocess(self) -> None:
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ), patch.object(
            subprocess, "Popen", side_effect=AssertionError("no subprocess")
        ):
            output, exit_code = run_operator_guidance_show("no_action_required")
        self.assertEqual(exit_code, 0)
        self.assertIn("runbook_ref:", output)

    def test_guidance_read_only_digest_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            digest_before = _hermes_digest(hermes_home)
            with patch.dict("os.environ", {"HERMES_HOME": str(hermes_home)}):
                _output, exit_code = run_operator_guidance_show(
                    "maintain_production_block"
                )
            digest_after = _hermes_digest(hermes_home)
        self.assertEqual(exit_code, 0)
        self.assertEqual(digest_before, digest_after)


class TestDispatchHelpConsistency(unittest.TestCase):
    def test_top_level_dispatch_help(self) -> None:
        output = _help_output([])
        self.assertIn(HELP_PRODUCTION_BLOCKED, output)
        self.assertIn(HELP_REPOSITORY_HARD_DENY, output)
        self.assertIn("operator guidance", output)
        self.assertIn("docs/operator/README.md", output)

    def test_gateway_help_tree(self) -> None:
        output = _help_output(["gateway"])
        self.assertIn(HELP_READ_ONLY, output)
        self.assertIn("dashboard", output)
        self.assertIn("correlation", output)
        self.assertIn("audit", output)

    def test_pilot_help_tree(self) -> None:
        output = _help_output(["pilot"])
        self.assertIn(HELP_PRODUCTION_BLOCKED, output)

    def test_production_help_tree(self) -> None:
        output = _help_output(["production"])
        self.assertIn("Read-only", output)
        self.assertIn("Execution remains disabled", output)

    def test_consume_repair_help_tree(self) -> None:
        output = _help_output(["consume", "repair"])
        self.assertIn("dry-run", output.lower())

    def test_read_only_vs_write_warnings(self) -> None:
        top_help = _help_output([])
        self.assertIn("status", top_help)
        self.assertIn(HELP_READ_ONLY, top_help)
        self.assertIn("run", top_help)
        self.assertIn("Operator confirmation required", top_help)
        self.assertIn(HELP_PRODUCTION_BLOCKED, top_help)

    def test_mock_pilot_help_includes_isolated_scope(self) -> None:
        output = _help_output(["gateway"])
        normalized = " ".join(output.split())
        self.assertIn("pilot", output)
        self.assertIn("Isolated mock scope only", normalized)
        self.assertIn("Production execution remains disabled", normalized)

    def test_repository_help_includes_hard_deny(self) -> None:
        output = _help_output([])
        self.assertIn("repository", output)
        self.assertIn(HELP_REPOSITORY_HARD_DENY, output)


class TestGuidanceOutputIntegration(unittest.TestCase):
    def test_discord_status_runbook_ref(self) -> None:
        result = DiscordGatewayStatusResult(
            session_id="sess-1",
            ticket_id="ticket-1",
            view="health",
            accepted=True,
            health_status="DEGRADED",
            recommended_action="resolve_recovery_required",
            failure_reason_code="(none)",
            gateway_state="staged",
        )
        rendered = format_discord_gateway_status_response(result)
        self.assertIn("runbook_ref: Recovery_Runbook#manual-recovery", rendered)
        self.assertIn("guidance_summary:", rendered)
        self.assertIn("production_execution_allowed: false", rendered)

    def test_dashboard_guidance_integration(self) -> None:
        output = format_operator_dashboard_summary(
            _dashboard_summary(recommended_action="stage_gateway")
        )
        self.assertIn("runbook_ref: Gateway_Runbook#gateway-state", output)
        self.assertIn("guidance_summary:", output)

    def test_correlation_diff_guidance_integration(self) -> None:
        diff = CooDispatchGatewayCorrelationDiff(
            left_gateway_request_id="gw-left",
            right_gateway_request_id="gw-right",
            same_ticket=True,
            same_session=True,
            changed_fields_count=0,
            changed_fields=(),
            health_transition="unchanged",
            consume_transition="unchanged",
            recovery_transition="unchanged",
            correlation_transition="unchanged",
            regression_detected=False,
            recommended_action="inspect_regression",
        )
        output = format_gateway_correlation_diff(diff)
        self.assertIn("runbook_ref: Operator_Checklist#gateway-incident", output)

    def test_correlation_chain_guidance_integration(self) -> None:
        output = format_gateway_correlation_chain(_correlation_chain())
        self.assertIn("runbook_ref: Recovery_Runbook#manual-recovery", output)


class TestOperatorDocumentationLinks(unittest.TestCase):
    def test_recommended_action_mapping_doc_exists(self) -> None:
        path = _OPERATOR_DOCS / "Recommended_Action_Mapping.md"
        self.assertTrue(path.is_file())

    def test_operator_index_links_mapping(self) -> None:
        index_text = (_OPERATOR_DOCS / "README.md").read_text(encoding="utf-8")
        self.assertIn("Recommended_Action_Mapping.md", index_text)

    def test_readme_links_mapping(self) -> None:
        readme = _README.read_text(encoding="utf-8")
        self.assertIn("Recommended_Action_Mapping.md", readme)

    def test_runbook_section_anchors_exist(self) -> None:
        anchor_re = re.compile(r"<!--\s*anchor:\s*([a-z0-9-]+)\s*-->")
        for filename, sections in RUNBOOK_SECTION_ANCHORS.items():
            text = (_OPERATOR_DOCS / filename).read_text(encoding="utf-8")
            found_anchors = set(anchor_re.findall(text))
            for section_id, heading_hint in sections.items():
                self.assertIn(
                    section_id,
                    found_anchors,
                    f"missing anchor {section_id!r} in {filename}",
                )
                self.assertIn(
                    heading_hint.lower(),
                    text.lower(),
                    f"missing heading hint {heading_hint!r} in {filename}",
                )

    def test_anchor_ids_unique_per_file(self) -> None:
        anchor_re = re.compile(r"<!--\s*anchor:\s*([a-z0-9-]+)\s*-->")
        for path in _OPERATOR_DOCS.glob("*.md"):
            anchors = anchor_re.findall(path.read_text(encoding="utf-8"))
            self.assertEqual(
                len(anchors),
                len(set(anchors)),
                f"duplicate anchors in {path.name}",
            )

    def test_mapping_doc_links_resolve(self) -> None:
        text = (_OPERATOR_DOCS / "Recommended_Action_Mapping.md").read_text(
            encoding="utf-8"
        )
        links = re.findall(r"\]\(([^)]+\.md)\)", text)
        for link in links:
            self.assertTrue(
                (_OPERATOR_DOCS / link).is_file(),
                f"broken mapping link: {link}",
            )


if __name__ == "__main__":
    unittest.main()
