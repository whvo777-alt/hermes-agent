"""Production executor factory — bounded subprocess wrapper (Phase 10N).

Builds a PipelineDispatchExecutor callable behind ProductionExecutorPolicy.
No Repository 2 execution unless a caller injects subprocess_runner in tests
or explicitly enables policy with an isolated allowlisted root.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from agent.coo.pipeline_adapter import PipelineDispatchExecutor
from agent.coo.production_executor_policy import ProductionExecutorPolicy
from hermes_constants import get_hermes_home

SubprocessRunner = Callable[
    [list[str], str, dict[str, str], int],
    Tuple[int, str, str],
]

_ALLOWED_FACTORY_ENTRYPOINT = "node pipeline.js"
_NPM_MARKERS = ("npm ", "npm run", "npx ", "npx run")
_BLOCKED_ENTRYPOINT_MARKERS = ("publish", "preflight")
_RUN_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DEFAULT_MAX_OUTPUT_BYTES = 64_000
_TIMEOUT_EXIT_CODE = 124


def default_evidence_dir() -> Path:
    return get_hermes_home() / "coo" / "execution-evidence"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_root(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _assert_root_allowed(pipeline_root: str, allowed_roots: tuple[str, ...]) -> str:
    if not allowed_roots:
        raise ValueError("allowed_pipeline_roots is empty")
    resolved = _normalize_root(pipeline_root)
    for allowed in allowed_roots:
        allowed_resolved = _normalize_root(allowed)
        try:
            common = os.path.commonpath([resolved, allowed_resolved])
        except ValueError:
            continue
        if common == allowed_resolved:
            return resolved
    raise ValueError(
        f"pipeline_root {pipeline_root!r} is outside allowed_pipeline_roots"
    )


def _assert_entrypoint_allowed(entrypoint: str, policy: ProductionExecutorPolicy) -> None:
    if entrypoint != _ALLOWED_FACTORY_ENTRYPOINT:
        raise ValueError(
            f"entrypoint {entrypoint!r} is not allowed; "
            f"only {_ALLOWED_FACTORY_ENTRYPOINT!r} is supported"
        )
    if entrypoint not in policy.allowed_entrypoints:
        raise ValueError(f"entrypoint {entrypoint!r} is not in allowed_entrypoints")

    normalized = entrypoint.strip().lower()
    if any(normalized.startswith(marker) for marker in _NPM_MARKERS):
        raise ValueError("npm entrypoints are not allowed")
    if any(marker in normalized for marker in _BLOCKED_ENTRYPOINT_MARKERS):
        raise ValueError("publish/preflight entrypoints are not allowed")


def _resolve_node_executable(node_path: str | None) -> str:
    if node_path:
        return node_path
    resolved = shutil.which("node")
    if not resolved:
        raise ValueError("Node executable not found.")
    return resolved


def _assert_run_date(run_date: str) -> None:
    normalized = run_date.strip()
    if not _RUN_DATE_PATTERN.match(normalized):
        raise ValueError(
            f"run_date {run_date!r} must match YYYY-MM-DD format"
        )
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            "run_date must be a valid calendar date in YYYY-MM-DD format."
        ) from exc


def _truncate_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{truncated}\n...[truncated]"


def _minimal_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _assert_evidence_path_within_hermes_home(
    resolved: Path,
    hermes_root: Path,
    *,
    label: str,
) -> None:
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ValueError(
            f"Evidence {label} {resolved} must remain under Hermes home {hermes_root}"
        ) from exc


def _evidence_paths(
    evidence_run_id: str,
    *,
    evidence_dir: Path | None = None,
) -> tuple[Path, Path, Path, Path]:
    base = evidence_dir or default_evidence_dir()
    hermes_root = get_hermes_home().resolve()
    resolved_base = base.resolve()
    stdout_path = base / f"{evidence_run_id}.stdout.log"
    stderr_path = base / f"{evidence_run_id}.stderr.log"
    meta_path = base / f"{evidence_run_id}.meta.json"
    for label, path in (
        ("directory", resolved_base),
        ("stdout path", stdout_path.resolve()),
        ("stderr path", stderr_path.resolve()),
        ("meta path", meta_path.resolve()),
    ):
        _assert_evidence_path_within_hermes_home(path, hermes_root, label=label)
    return resolved_base, stdout_path, stderr_path, meta_path


def _write_evidence_files(
    *,
    evidence_run_id: str,
    execution_attempt_id: str,
    argv: list[str],
    cwd: str,
    timeout_seconds: int,
    started_at: str,
    finished_at: str,
    exit_code: int,
    stdout_text: str,
    stderr_text: str,
    evidence_dir: Path | None = None,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> dict[str, str]:
    base, stdout_path, stderr_path, meta_path = _evidence_paths(
        evidence_run_id,
        evidence_dir=evidence_dir,
    )
    base.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")
    meta = {
        "execution_attempt_id": execution_attempt_id,
        "argv": list(argv),
        "cwd": cwd,
        "timeout_seconds": timeout_seconds,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "meta_log": str(meta_path),
    }


def _run_bounded_subprocess(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_text = _truncate_text(exc.stdout or "", max_output_bytes)
        stderr_text = _truncate_text(
            (exc.stderr or "") + f"\ntimeout after {timeout_seconds}s",
            max_output_bytes,
        )
        return _TIMEOUT_EXIT_CODE, stdout_text, stderr_text

    stdout_text = _truncate_text(completed.stdout or "", max_output_bytes)
    stderr_text = _truncate_text(completed.stderr or "", max_output_bytes)
    return completed.returncode, stdout_text, stderr_text


def build_pipeline_dispatch_executor(
    policy: ProductionExecutorPolicy,
    *,
    pipeline_root: str,
    entrypoint: str,
    evidence_run_id: str = "",
    execution_attempt_id: str = "",
    subprocess_runner: SubprocessRunner | None = None,
    node_path: str | None = None,
    evidence_dir: Path | None = None,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> PipelineDispatchExecutor:
    """Build a bounded executor callable for PipelineAdapter injection."""
    if not policy.enabled:
        raise ValueError("production executor policy is disabled")

    resolved_root = _assert_root_allowed(pipeline_root, policy.allowed_pipeline_roots)
    _assert_entrypoint_allowed(entrypoint, policy)

    if policy.max_runtime_seconds <= 0:
        raise ValueError("max_runtime_seconds must be positive")

    node_executable = _resolve_node_executable(node_path)
    resolved_execution_attempt_id = execution_attempt_id.strip() or str(uuid.uuid4())
    resolved_evidence_run_id = evidence_run_id.strip() or resolved_execution_attempt_id
    timeout_seconds = policy.max_runtime_seconds
    env = _minimal_env()

    if evidence_dir is not None:
        _evidence_paths(resolved_evidence_run_id, evidence_dir=evidence_dir)

    def _executor(
        _entrypoint: str,
        _pipeline_root: str,
        run_date: str,
    ) -> tuple[int, str, str]:
        if _entrypoint != _ALLOWED_FACTORY_ENTRYPOINT:
            raise ValueError(
                f"entrypoint {_entrypoint!r} is not allowed at execution time"
            )
        if _normalize_root(_pipeline_root) != resolved_root:
            raise ValueError(
                f"pipeline_root {_pipeline_root!r} does not match factory root {resolved_root!r}"
            )
        _assert_run_date(run_date)
        argv = [node_executable, "pipeline.js", "--run-date", run_date.strip()]
        started_at = _utc_now_iso()
        if subprocess_runner is not None:
            exit_code, stdout_text, stderr_text = subprocess_runner(
                argv,
                resolved_root,
                env,
                timeout_seconds,
            )
            stdout_text = _truncate_text(stdout_text, max_output_bytes)
            stderr_text = _truncate_text(stderr_text, max_output_bytes)
        else:
            exit_code, stdout_text, stderr_text = _run_bounded_subprocess(
                argv,
                cwd=resolved_root,
                env=env,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )
        finished_at = _utc_now_iso()
        _write_evidence_files(
            evidence_run_id=resolved_evidence_run_id,
            execution_attempt_id=resolved_execution_attempt_id,
            argv=argv,
            cwd=resolved_root,
            timeout_seconds=timeout_seconds,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=exit_code,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            evidence_dir=evidence_dir,
            max_output_bytes=max_output_bytes,
        )
        return exit_code, stdout_text, stderr_text

    return _executor
