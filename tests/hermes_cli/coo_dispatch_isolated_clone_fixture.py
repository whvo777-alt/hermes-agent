"""Reusable isolated clone-shaped pipeline fixtures for Phase 12I drill tests."""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.coo.dispatch_cli_run import execute_coo_dispatch_run
from tests.hermes_cli.coo_dispatch_isolated_fixture import (
    NODE_BEHAVIOR_ENV_PROBE,
    NODE_BEHAVIOR_FAILURE,
    NODE_BEHAVIOR_PARTIAL,
    NODE_BEHAVIOR_SUCCESS,
    NODE_BEHAVIOR_TIMEOUT,
    NODE_BEHAVIOR_VERBOSE,
    _DEFAULT_RUN_DATE,
    bounded_dispatch_config,
    expected_factory_argv,
    list_audit_records,
    list_evidence_meta,
    resolve_isolated_dispatch_runner,
    run_isolated_full_path_execute,
    run_kwargs,
    write_fake_node_executable,
)
from tests.hermes_cli.test_coo_dispatch_run import _CooDispatchRunFixture

CLONE_BEHAVIOR_SUCCESS = NODE_BEHAVIOR_SUCCESS
CLONE_BEHAVIOR_FAILURE = NODE_BEHAVIOR_FAILURE
CLONE_BEHAVIOR_TIMEOUT = NODE_BEHAVIOR_TIMEOUT
CLONE_BEHAVIOR_PARTIAL = NODE_BEHAVIOR_PARTIAL
CLONE_BEHAVIOR_VERBOSE = NODE_BEHAVIOR_VERBOSE
CLONE_BEHAVIOR_ENV_PROBE = NODE_BEHAVIOR_ENV_PROBE


@dataclass(frozen=True)
class IsolatedClonePaths:
    root: Path
    pipeline_js: Path
    package_json: Path
    config_dir: Path
    outputs_dir: Path
    reports_dir: Path
    fixture_state: Path


def build_isolated_clone_tree(
    clone_root: Path,
    *,
    run_date: str = _DEFAULT_RUN_DATE,
) -> IsolatedClonePaths:
    """Create a clone-shaped fixture tree without copying Repository2 content."""
    config_dir = clone_root / "config"
    outputs_dir = clone_root / "outputs"
    reports_dir = clone_root / "reports"
    for path in (config_dir, outputs_dir, reports_dir):
        path.mkdir(parents=True, exist_ok=True)

    pipeline_js = clone_root / "pipeline.js"
    pipeline_js.write_text(
        textwrap.dedent(
            """
            // isolated clone fixture entrypoint (not Repository2 code)
            module.exports = { entrypoint: "pipeline.js" };
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    package_json = clone_root / "package.json"
    package_json.write_text(
        json.dumps(
            {
                "name": "isolated-clone-fixture",
                "private": True,
                "description": "structure-only fixture; npm is never invoked",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (config_dir / "pipeline.json").write_text(
        json.dumps({"fixture": True, "run_date": run_date}, indent=2) + "\n",
        encoding="utf-8",
    )

    fixture_state = clone_root / "fixture_state.json"
    fixture_state.write_text(
        json.dumps({"runs": [], "last_run_date": ""}, indent=2) + "\n",
        encoding="utf-8",
    )

    return IsolatedClonePaths(
        root=clone_root,
        pipeline_js=pipeline_js,
        package_json=package_json,
        config_dir=config_dir,
        outputs_dir=outputs_dir,
        reports_dir=reports_dir,
        fixture_state=fixture_state,
    )


def write_clone_fake_node(workspace: Path, behavior: str = CLONE_BEHAVIOR_SUCCESS) -> Path:
    """Fake node wrapper that understands clone output/report directories."""
    bin_dir = workspace / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    node_path = bin_dir / "node"

    if behavior == CLONE_BEHAVIOR_SUCCESS:
        body = textwrap.dedent(
            """
            import json
            import os
            import sys
            from datetime import datetime, timezone
            from pathlib import Path

            args = sys.argv
            run_date = args[args.index("--run-date") + 1]
            cwd = Path(os.getcwd())
            outputs = cwd / "outputs"
            reports = cwd / "reports"
            outputs.mkdir(exist_ok=True)
            reports.mkdir(exist_ok=True)

            payload = {"run_date": run_date, "status": "completed"}
            (outputs / f"{run_date}.json").write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            (reports / f"{run_date}.json").write_text(
                json.dumps({"report": payload}, indent=2),
                encoding="utf-8",
            )

            state_path = cwd / "fixture_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["runs"].append(
                {
                    "run_date": run_date,
                    "status": "completed",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            state["last_run_date"] = run_date
            state_path.write_text(json.dumps(state, indent=2) + "\\n", encoding="utf-8")
            print("clone-ok")
            sys.exit(0)
            """
        )
    else:
        return write_fake_node_executable(workspace, behavior)

    node_path.write_text(f"#!/usr/bin/env python3\n{body}", encoding="utf-8")
    node_path.chmod(0o755)
    return node_path.resolve()


class CooDispatchIsolatedCloneFixture(_CooDispatchRunFixture):
    """Dispatch run fixture rooted at an isolated clone-shaped pipeline tree."""

    def __init__(self) -> None:
        super().__init__()
        self.pipeline_root = Path(self.tmp.name) / "isolated-clone"
        self.pipeline_root.mkdir(parents=True, exist_ok=True)
        self.clone_paths = build_isolated_clone_tree(self.pipeline_root)


def clone_output_path(clone_root: Path, run_date: str = _DEFAULT_RUN_DATE) -> Path:
    return clone_root / "outputs" / f"{run_date}.json"


def clone_report_path(clone_root: Path, run_date: str = _DEFAULT_RUN_DATE) -> Path:
    return clone_root / "reports" / f"{run_date}.json"


def clone_partial_output_path(clone_root: Path, run_date: str = _DEFAULT_RUN_DATE) -> Path:
    return clone_root / "outputs" / f"{run_date}.partial.json"


def run_clone_full_path_execute(
    fixture: CooDispatchIsolatedCloneFixture,
    seeded: dict,
    *,
    node_behavior: str = CLONE_BEHAVIOR_SUCCESS,
    fake_node: Path | None = None,
    harness_max_output_bytes: int = 64_000,
    harness_max_timeout_seconds: int = 3600,
    policy_max_runtime_seconds: int | None = None,
    **run_overrides,
):
    """Full-path execute against clone-shaped fixture via dispatch harness."""
    workspace = fixture.pipeline_root.parent
    fixture.clone_paths = build_isolated_clone_tree(fixture.pipeline_root)
    node = fake_node or write_clone_fake_node(workspace, node_behavior)
    return run_isolated_full_path_execute(
        fixture,
        seeded,
        fake_node=node,
        harness_max_output_bytes=harness_max_output_bytes,
        harness_max_timeout_seconds=harness_max_timeout_seconds,
        policy_max_runtime_seconds=policy_max_runtime_seconds,
        use_existing_pipeline_js=True,
        **run_overrides,
    )


__all__ = (
    "CLONE_BEHAVIOR_ENV_PROBE",
    "CLONE_BEHAVIOR_FAILURE",
    "CLONE_BEHAVIOR_PARTIAL",
    "CLONE_BEHAVIOR_SUCCESS",
    "CLONE_BEHAVIOR_TIMEOUT",
    "CLONE_BEHAVIOR_VERBOSE",
    "CooDispatchIsolatedCloneFixture",
    "IsolatedClonePaths",
    "_DEFAULT_RUN_DATE",
    "bounded_dispatch_config",
    "build_isolated_clone_tree",
    "clone_output_path",
    "clone_partial_output_path",
    "clone_report_path",
    "expected_factory_argv",
    "list_audit_records",
    "list_evidence_meta",
    "run_clone_full_path_execute",
    "run_kwargs",
    "write_clone_fake_node",
)
