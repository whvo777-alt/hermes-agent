"""Reusable isolated /tmp pipeline fixtures for COO dispatch full-path drill tests."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import textwrap
from pathlib import Path
from typing import Any

from agent.coo.bounded_subprocess_runner import RUNNER_PROFILE_DISPATCH
from agent.coo.dispatch_cli_run import execute_coo_dispatch_run
from agent.coo.dispatch_cli_runner_injection import resolve_bounded_subprocess_runner
from agent.coo.dispatch_runner_binding_state import (
    RUNNER_BINDING_STATE_BOUND,
    CooDispatchRunnerBindingState,
)
from agent.coo.production_executor_factory import _TIMEOUT_EXIT_CODE
from hermes_cli.coo_dispatch import run_coo_dispatch_from_args
from tests.hermes_cli.test_coo_dispatch_run import (
    _CooDispatchRunFixture,
    _enabled_executor_config,
)

NODE_BEHAVIOR_SUCCESS = "success"
NODE_BEHAVIOR_FAILURE = "failure"
NODE_BEHAVIOR_TIMEOUT = "timeout"
NODE_BEHAVIOR_VERBOSE = "verbose"
NODE_BEHAVIOR_ENV_PROBE = "env_probe"
NODE_BEHAVIOR_PARTIAL = "partial"

_DEFAULT_RUN_DATE = "2026-07-07"
_DEFAULT_HARNESS_MAX_TIMEOUT_SECONDS = 3600


def bounded_dispatch_config(pipeline_root: Path) -> dict[str, Any]:
    """Executor + bounded runner provider config for isolated drill tests."""
    config = _enabled_executor_config(pipeline_root)
    config["coo"]["dispatch"]["runner_provider"] = {"mode": "bounded"}
    return config


def write_fake_pipeline_js(pipeline_root: Path) -> Path:
    """Create a minimal pipeline.js fixture (not Repository2 code)."""
    script = pipeline_root / "pipeline.js"
    script.write_text("// isolated test fixture only\n", encoding="utf-8")
    return script


def write_fake_node_executable(workspace: Path, behavior: str = NODE_BEHAVIOR_SUCCESS) -> Path:
    """Create a basename-`node` Python shebang wrapper with controlled behavior."""
    bin_dir = workspace / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    node_path = bin_dir / "node"

    if behavior == NODE_BEHAVIOR_SUCCESS:
        body = textwrap.dedent(
            """
            import sys
            print("fixture-ok")
            sys.exit(0)
            """
        )
    elif behavior == NODE_BEHAVIOR_FAILURE:
        body = textwrap.dedent(
            """
            import sys
            sys.stderr.write("fake failure")
            sys.exit(3)
            """
        )
    elif behavior == NODE_BEHAVIOR_TIMEOUT:
        body = textwrap.dedent(
            """
            import time
            time.sleep(30)
            """
        )
    elif behavior == NODE_BEHAVIOR_VERBOSE:
        body = textwrap.dedent(
            """
            print("x" * 100000)
            """
        )
    elif behavior == NODE_BEHAVIOR_ENV_PROBE:
        body = textwrap.dedent(
            """
            import os
            print(
                "SECRET_TOKEN" in os.environ,
                "API_KEY" in os.environ,
            )
            """
        )
    elif behavior == NODE_BEHAVIOR_PARTIAL:
        body = textwrap.dedent(
            """
            import json
            import os
            import sys
            from pathlib import Path

            args = sys.argv
            run_date = args[args.index("--run-date") + 1]
            cwd = Path(os.getcwd())
            outputs = cwd / "outputs"
            outputs.mkdir(exist_ok=True)
            (outputs / f"{run_date}.partial.json").write_text(
                json.dumps({"partial": True, "run_date": run_date}),
                encoding="utf-8",
            )
            sys.stderr.write("partial failure")
            sys.exit(4)
            """
        )
    else:
        raise ValueError(f"unknown fake node behavior: {behavior!r}")

    node_path.write_text(f"#!/usr/bin/env python3\n{body}", encoding="utf-8")
    node_path.chmod(0o755)
    return node_path.resolve()


def expected_factory_argv(fake_node: Path, run_date: str = _DEFAULT_RUN_DATE) -> list[str]:
    return [str(fake_node), "pipeline.js", "--run-date", run_date]


def resolve_isolated_dispatch_runner(
    *,
    pipeline_root: Path,
    fake_node: Path,
    merged_config: dict[str, Any] | None = None,
    harness_max_output_bytes: int = 64_000,
    harness_max_timeout_seconds: int = _DEFAULT_HARNESS_MAX_TIMEOUT_SECONDS,
):
    config = merged_config or bounded_dispatch_config(pipeline_root)
    return resolve_bounded_subprocess_runner(
        config,
        binding_state=CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_BOUND),
        use_real_bounded_runner=True,
        harness_profile=RUNNER_PROFILE_DISPATCH,
        node_executable=str(fake_node),
        harness_max_output_bytes=harness_max_output_bytes,
        harness_max_timeout_seconds=harness_max_timeout_seconds,
    )


def build_run_args(seeded: dict, pipeline_root: Path, *, dry_run: bool = False) -> argparse.Namespace:
    ticket = seeded["ticket"]
    prepare = seeded["prepare"]
    confirmation = seeded["confirmation"]
    return argparse.Namespace(
        ticket_id=ticket.ticket_id,
        confirmation_id=confirmation.confirmation_id,
        unlock_token_id=prepare["unlock_token"]["token_id"],
        requester_id=ticket.requester_id,
        pipeline_root=str(pipeline_root),
        dry_run=dry_run,
    )


def run_kwargs(
    fixture: _CooDispatchRunFixture,
    seeded: dict,
    *,
    pipeline_root: Path | None = None,
    merged_config: dict[str, Any] | None = None,
    **overrides,
) -> dict[str, Any]:
    ticket = seeded["ticket"]
    prepare = seeded["prepare"]
    confirmation = seeded["confirmation"]
    root = pipeline_root or fixture.pipeline_root
    base = dict(
        ticket_id=ticket.ticket_id,
        confirmation_id=confirmation.confirmation_id,
        unlock_token_id=prepare["unlock_token"]["token_id"],
        requester_id=ticket.requester_id,
        pipeline_root=str(root),
        bundle_dir=fixture.bundle_dir,
        confirmation_dir=fixture.confirmation_dir,
        audit_dir=fixture.hermes_home / "coo" / "audit",
        evidence_dir=fixture.hermes_home / "coo" / "execution-evidence",
        merged_config=merged_config or bounded_dispatch_config(root),
    )
    base.update(overrides)
    return base


def run_isolated_full_path_execute(
    fixture: _CooDispatchRunFixture,
    seeded: dict,
    *,
    node_behavior: str = NODE_BEHAVIOR_SUCCESS,
    fake_node: Path | None = None,
    harness_max_output_bytes: int = 64_000,
    harness_max_timeout_seconds: int = _DEFAULT_HARNESS_MAX_TIMEOUT_SECONDS,
    policy_max_runtime_seconds: int | None = None,
    use_existing_pipeline_js: bool = False,
    **run_overrides,
):
    """Run execute_coo_dispatch_run through provider-resolved dispatch harness."""
    workspace = fixture.pipeline_root.parent
    if not use_existing_pipeline_js:
        write_fake_pipeline_js(fixture.pipeline_root)
    node = fake_node or write_fake_node_executable(workspace, node_behavior)
    runner = resolve_isolated_dispatch_runner(
        pipeline_root=fixture.pipeline_root,
        fake_node=node,
        merged_config=run_overrides.get("merged_config")
        or bounded_dispatch_config(fixture.pipeline_root),
        harness_max_output_bytes=harness_max_output_bytes,
        harness_max_timeout_seconds=harness_max_timeout_seconds,
    )
    kwargs = run_kwargs(fixture, seeded, **run_overrides)
    kwargs["subprocess_runner"] = runner
    kwargs["node_path"] = str(node)

    if policy_max_runtime_seconds is not None:
        from unittest.mock import patch

        import agent.coo.dispatch_cli_run as dispatch_cli_run

        original = dispatch_cli_run._resolve_run_executor_policy

        def _short_policy(*, pipeline_root, merged_config):
            policy = original(
                pipeline_root=pipeline_root,
                merged_config=merged_config,
            )
            return dataclasses.replace(
                policy,
                max_runtime_seconds=policy_max_runtime_seconds,
            )

        with patch.object(
            dispatch_cli_run,
            "_resolve_run_executor_policy",
            _short_policy,
        ):
            return execute_coo_dispatch_run(**kwargs)

    return execute_coo_dispatch_run(**kwargs)


def run_isolated_full_path_from_args(
    fixture: _CooDispatchRunFixture,
    seeded: dict,
    *,
    node_behavior: str = NODE_BEHAVIOR_SUCCESS,
    fake_node: Path | None = None,
    harness_max_output_bytes: int = 64_000,
    harness_max_timeout_seconds: int = _DEFAULT_HARNESS_MAX_TIMEOUT_SECONDS,
    **overrides,
) -> int:
    """Run run_coo_dispatch_from_args with provider dispatch-profile harness opt-in."""
    workspace = fixture.pipeline_root.parent
    write_fake_pipeline_js(fixture.pipeline_root)
    node = fake_node or write_fake_node_executable(workspace, node_behavior)
    config = overrides.pop("merged_config", bounded_dispatch_config(fixture.pipeline_root))
    args = build_run_args(seeded, fixture.pipeline_root)
    return run_coo_dispatch_from_args(
        args,
        use_runner_provider=True,
        use_real_bounded_runner=True,
        harness_profile=RUNNER_PROFILE_DISPATCH,
        node_executable=str(node),
        node_path=str(node),
        merged_config=config,
        binding_state=CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_BOUND),
        harness_max_output_bytes=harness_max_output_bytes,
        harness_max_timeout_seconds=harness_max_timeout_seconds,
        **overrides,
    )


def list_audit_records(audit_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not audit_dir.is_dir():
        return records
    for path in sorted(audit_dir.glob("*.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def list_evidence_meta(evidence_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not evidence_dir.is_dir():
        return records
    for path in sorted(evidence_dir.glob("*.meta.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def evidence_stdout_paths(evidence_dir: Path) -> list[Path]:
    if not evidence_dir.is_dir():
        return []
    return sorted(evidence_dir.glob("*.stdout.log"))


def assert_subprocess_argv_contract(
    meta: dict[str, Any],
    *,
    fake_node: Path,
    pipeline_root: Path,
    run_date: str = _DEFAULT_RUN_DATE,
) -> None:
    argv = meta["argv"]
    cwd = meta["cwd"]
    assert argv == expected_factory_argv(fake_node, run_date)
    assert cwd == os.path.realpath(str(pipeline_root))
    assert meta["exit_code"] is not None


__all__ = (
    "NODE_BEHAVIOR_ENV_PROBE",
    "NODE_BEHAVIOR_FAILURE",
    "NODE_BEHAVIOR_PARTIAL",
    "NODE_BEHAVIOR_SUCCESS",
    "NODE_BEHAVIOR_TIMEOUT",
    "NODE_BEHAVIOR_VERBOSE",
    "_DEFAULT_RUN_DATE",
    "_TIMEOUT_EXIT_CODE",
    "assert_subprocess_argv_contract",
    "bounded_dispatch_config",
    "build_run_args",
    "expected_factory_argv",
    "evidence_stdout_paths",
    "list_audit_records",
    "list_evidence_meta",
    "resolve_isolated_dispatch_runner",
    "run_isolated_full_path_execute",
    "run_isolated_full_path_from_args",
    "run_kwargs",
    "write_fake_node_executable",
    "write_fake_pipeline_js",
)
