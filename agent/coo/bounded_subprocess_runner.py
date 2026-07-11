"""Bounded subprocess runner safety harness — Phase 12E-4.

Builds an explicitly created SubprocessRunner callable with fail-closed argv, cwd,
env, and timeout validation. Intended for service/internal opt-in and isolated
/tmp harness tests only. Never auto-wired into the default CLI run path.
"""

from __future__ import annotations

import os
import subprocess
from typing import Sequence

from agent.coo.dispatch_pipeline_root_trust import assert_pipeline_root_allowed
from agent.coo.production_executor_factory import (
    SubprocessRunner,
    _TIMEOUT_EXIT_CODE,
)

_DEFAULT_MAX_OUTPUT_BYTES = 64_000
_DEFAULT_MAX_TIMEOUT_SECONDS = 300

_ALLOWED_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ"})
_BLOCKED_EXECUTABLES = frozenset({"node", "npm", "npx", "bash", "sh", "dash", "zsh"})


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


def _validate_argv(argv: object) -> list[str]:
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
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    max_timeout_seconds: int = _DEFAULT_MAX_TIMEOUT_SECONDS,
) -> SubprocessRunner:
    """Create a bounded subprocess runner for explicit service/test injection only."""
    if not allowed_pipeline_roots:
        raise BoundedSubprocessRunnerError("allowed_pipeline_roots is required")
    if max_output_bytes <= 0:
        raise BoundedSubprocessRunnerError("max_output_bytes must be positive")
    if max_timeout_seconds <= 0:
        raise BoundedSubprocessRunnerError("max_timeout_seconds must be positive")

    allowed_roots = tuple(_normalize_root(path) for path in allowed_pipeline_roots)
    for root in allowed_roots:
        assert_pipeline_root_allowed(root)

    def _runner(
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> tuple[int, str, str]:
        validated_argv = _validate_argv(argv)
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
