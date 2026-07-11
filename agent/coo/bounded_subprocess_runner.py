"""Bounded subprocess runner safety harness — Phase 12E-4 / 12G.

Builds an explicitly created SubprocessRunner callable with fail-closed argv, cwd,
env, and timeout validation. Intended for service/internal opt-in and isolated
/tmp harness tests only. Never auto-wired into the default CLI run path.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from typing import Sequence

from agent.coo.dispatch_pipeline_root_trust import assert_pipeline_root_allowed
from agent.coo.production_executor_factory import (
    SubprocessRunner,
    _TIMEOUT_EXIT_CODE,
)

_DEFAULT_MAX_OUTPUT_BYTES = 64_000
_DEFAULT_MAX_TIMEOUT_SECONDS = 300

RUNNER_PROFILE_RESTRICTED = "restricted"
RUNNER_PROFILE_DISPATCH = "dispatch"

_ALLOWED_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ"})
_BLOCKED_EXECUTABLES = frozenset({"node", "npm", "npx", "bash", "sh", "dash", "zsh"})
_BLOCKED_DISPATCH_ALTERNATIVE_RUNTIMES = frozenset(
    {"npm", "npx", "bun", "deno", "bash", "sh", "dash", "zsh"}
)
_RUN_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KNOWN_RUNNER_PROFILES = frozenset(
    {RUNNER_PROFILE_RESTRICTED, RUNNER_PROFILE_DISPATCH}
)

__all__ = (
    "BoundedSubprocessRunnerError",
    "RUNNER_PROFILE_DISPATCH",
    "RUNNER_PROFILE_RESTRICTED",
    "create_bounded_subprocess_runner",
    "validate_dispatch_runner_argv_contract",
)


class BoundedSubprocessRunnerError(ValueError):
    """Raised when bounded subprocess runner input validation fails."""


def _truncate_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{truncated}\n...[truncated]"


def _normalize_root(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _assert_run_date(run_date: str) -> None:
    normalized = run_date.strip()
    if not _RUN_DATE_PATTERN.match(normalized):
        raise BoundedSubprocessRunnerError(
            "run_date must match YYYY-MM-DD format"
        )
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise BoundedSubprocessRunnerError(
            "run_date must be a valid calendar date in YYYY-MM-DD format"
        ) from exc


def _resolve_dispatch_node_executable(node_executable: str) -> str:
    if not isinstance(node_executable, str) or not node_executable.strip():
        raise BoundedSubprocessRunnerError(
            "node_executable is required for dispatch profile"
        )
    expanded = os.path.expanduser(node_executable.strip())
    if not os.path.isabs(expanded):
        raise BoundedSubprocessRunnerError(
            "node_executable must be an absolute path"
        )
    if not os.path.isfile(expanded):
        raise BoundedSubprocessRunnerError(
            "node_executable must be an existing file"
        )
    resolved = os.path.realpath(expanded)
    if not os.path.isfile(resolved):
        raise BoundedSubprocessRunnerError(
            "node_executable must be an existing file"
        )
    basename = os.path.basename(resolved).lower()
    if basename in _BLOCKED_DISPATCH_ALTERNATIVE_RUNTIMES:
        raise BoundedSubprocessRunnerError("node_executable is not allowed")
    if basename != "node":
        raise BoundedSubprocessRunnerError(
            "node_executable basename must be node"
        )
    return resolved


def _assert_argv_node_matches(argv_node: str, allowed_node_executable: str) -> None:
    if not isinstance(argv_node, str) or not argv_node.strip():
        raise BoundedSubprocessRunnerError(
            "argv executable must be a non-empty string"
        )
    expanded = os.path.expanduser(argv_node.strip())
    if not os.path.isabs(expanded):
        raise BoundedSubprocessRunnerError(
            "argv executable must be an absolute path"
        )
    if not os.path.isfile(expanded):
        raise BoundedSubprocessRunnerError(
            "argv executable must be an existing file"
        )
    resolved_argv = os.path.realpath(expanded)
    basename = os.path.basename(resolved_argv).lower()
    if basename in _BLOCKED_DISPATCH_ALTERNATIVE_RUNTIMES:
        raise BoundedSubprocessRunnerError("argv executable is not allowed")
    if basename != "node":
        raise BoundedSubprocessRunnerError("argv executable basename must be node")
    if resolved_argv != allowed_node_executable:
        raise BoundedSubprocessRunnerError(
            "argv executable does not match configured node executable"
        )


def _validate_restricted_argv(argv: object) -> list[str]:
    if not isinstance(argv, list) or not argv:
        raise BoundedSubprocessRunnerError("argv must be a non-empty list of strings")
    normalized: list[str] = []
    for item in argv:
        if not isinstance(item, str) or not item.strip():
            raise BoundedSubprocessRunnerError("argv entries must be non-empty strings")
        normalized.append(item)
    executable = os.path.basename(normalized[0]).lower()
    if executable in _BLOCKED_EXECUTABLES:
        raise BoundedSubprocessRunnerError("executable is not allowed in bounded harness")
    joined = " ".join(normalized).lower()
    if "pipeline.js" in joined or joined.strip().startswith("npm ") or " npx " in joined:
        raise BoundedSubprocessRunnerError("blocked command is not allowed in bounded harness")
    return normalized


def _validate_dispatch_argv(argv: object, allowed_node_executable: str) -> list[str]:
    if not isinstance(argv, list):
        raise BoundedSubprocessRunnerError("argv must be a list of strings")
    if len(argv) != 4:
        raise BoundedSubprocessRunnerError(
            "dispatch argv must contain exactly 4 elements"
        )
    normalized: list[str] = []
    for item in argv:
        if not isinstance(item, str) or not item.strip():
            raise BoundedSubprocessRunnerError("argv entries must be non-empty strings")
        normalized.append(item.strip())

    _assert_argv_node_matches(normalized[0], allowed_node_executable)
    if normalized[1] != "pipeline.js":
        raise BoundedSubprocessRunnerError("dispatch argv[1] must be pipeline.js")
    if normalized[2] != "--run-date":
        raise BoundedSubprocessRunnerError("dispatch argv[2] must be --run-date")
    _assert_run_date(normalized[3])
    return normalized


def validate_dispatch_runner_argv_contract(
    argv: object,
    *,
    node_executable: str,
) -> list[str]:
    """Validate factory-shaped argv against dispatch profile rules without executing."""
    resolved_node = _resolve_dispatch_node_executable(node_executable)
    return _validate_dispatch_argv(argv, resolved_node)


def _filter_env(env: object) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise BoundedSubprocessRunnerError("env must be a mapping")
    filtered: dict[str, str] = {}
    for key in _ALLOWED_ENV_KEYS:
        value = env.get(key)
        if isinstance(value, str) and value:
            filtered[key] = value
    return filtered


def _validate_timeout(timeout_seconds: object, *, max_timeout_seconds: int) -> int:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        raise BoundedSubprocessRunnerError("timeout_seconds must be a positive integer")
    if timeout_seconds <= 0:
        raise BoundedSubprocessRunnerError("timeout_seconds must be positive")
    if timeout_seconds > max_timeout_seconds:
        raise BoundedSubprocessRunnerError("timeout_seconds exceeds harness limit")
    return timeout_seconds


def _assert_cwd_allowed(cwd: str, allowed_pipeline_roots: Sequence[str]) -> str:
    if not isinstance(cwd, str) or not cwd.strip():
        raise BoundedSubprocessRunnerError("cwd must be a non-empty string")
    if not allowed_pipeline_roots:
        raise BoundedSubprocessRunnerError("allowed_pipeline_roots is empty")
    resolved_cwd = _normalize_root(cwd)
    assert_pipeline_root_allowed(resolved_cwd)
    for allowed in allowed_pipeline_roots:
        allowed_resolved = _normalize_root(allowed)
        assert_pipeline_root_allowed(allowed_resolved)
        try:
            if os.path.commonpath([resolved_cwd, allowed_resolved]) == allowed_resolved:
                return resolved_cwd
        except ValueError:
            continue
    raise BoundedSubprocessRunnerError("cwd is outside allowed_pipeline_roots")


def create_bounded_subprocess_runner(
    allowed_pipeline_roots: Sequence[str],
    *,
    profile: str = RUNNER_PROFILE_RESTRICTED,
    node_executable: str | None = None,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    max_timeout_seconds: int = _DEFAULT_MAX_TIMEOUT_SECONDS,
) -> SubprocessRunner:
    """Create a bounded subprocess runner for explicit service/test injection only."""
    if profile not in _KNOWN_RUNNER_PROFILES:
        raise BoundedSubprocessRunnerError(f"unknown runner profile: {profile!r}")
    if not allowed_pipeline_roots:
        raise BoundedSubprocessRunnerError("allowed_pipeline_roots is required")
    if max_output_bytes <= 0:
        raise BoundedSubprocessRunnerError("max_output_bytes must be positive")
    if max_timeout_seconds <= 0:
        raise BoundedSubprocessRunnerError("max_timeout_seconds must be positive")

    resolved_dispatch_node: str | None = None
    if profile == RUNNER_PROFILE_DISPATCH:
        if node_executable is None:
            raise BoundedSubprocessRunnerError(
                "node_executable is required for dispatch profile"
            )
        resolved_dispatch_node = _resolve_dispatch_node_executable(node_executable)
    elif node_executable is not None:
        raise BoundedSubprocessRunnerError(
            "node_executable is only valid for dispatch profile"
        )

    allowed_roots = tuple(_normalize_root(path) for path in allowed_pipeline_roots)
    for root in allowed_roots:
        assert_pipeline_root_allowed(root)

    def _runner(
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> tuple[int, str, str]:
        if profile == RUNNER_PROFILE_DISPATCH:
            assert resolved_dispatch_node is not None
            validated_argv = _validate_dispatch_argv(argv, resolved_dispatch_node)
        else:
            validated_argv = _validate_restricted_argv(argv)
        resolved_cwd = _assert_cwd_allowed(cwd, allowed_roots)
        filtered_env = _filter_env(env)
        timeout = _validate_timeout(
            timeout_seconds,
            max_timeout_seconds=max_timeout_seconds,
        )
        try:
            completed = subprocess.run(
                validated_argv,
                cwd=resolved_cwd,
                env=filtered_env,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_text = _truncate_text(exc.stdout or "", max_output_bytes)
            stderr_text = _truncate_text(
                (exc.stderr or "") + f"\ntimeout after {timeout}s",
                max_output_bytes,
            )
            return _TIMEOUT_EXIT_CODE, stdout_text, stderr_text

        stdout_text = _truncate_text(completed.stdout or "", max_output_bytes)
        stderr_text = _truncate_text(completed.stderr or "", max_output_bytes)
        return completed.returncode, stdout_text, stderr_text

    return _runner
