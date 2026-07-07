"""Terminal guard — block direct Repository 2 execution during COO approval phases."""

from __future__ import annotations

import os
import re
from typing import Optional, Sequence, Tuple

REPOSITORY2_BLOCK_MESSAGE = (
    "Repository2 execution is blocked during COO approval phases. "
    "Use COO approval flow only."
)

_DEFAULT_REPOSITORY2_ROOT = "/opt/data/multi-content-pipeline"

# (pattern, human label) — matched case-insensitively against the command string.
_BLOCKED_COMMAND_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"node\s+pipeline\.js\b", re.I), "node pipeline.js"),
    (re.compile(r"\bnpm\s+run\b", re.I), "npm run"),
    (re.compile(r"\bnpm\s+test\b", re.I), "npm test"),
    (re.compile(r"\bpublish\b", re.I), "publish"),
    (re.compile(r"\bpreflight\b", re.I), "preflight"),
)


def get_repository2_root() -> str:
    """Return the configured Repository 2 root path."""
    return os.environ.get("CONTENT_PIPELINE_ROOT", _DEFAULT_REPOSITORY2_ROOT)


def _normalize_path(path: str) -> str:
    return os.path.normpath(path.strip().rstrip("/"))


def _path_touches_repository2(
    *,
    command: str,
    effective_workdir: Optional[str],
    root: str,
) -> bool:
    root_norm = _normalize_path(root)
    if effective_workdir:
        workdir_norm = _normalize_path(effective_workdir)
        if workdir_norm == root_norm or workdir_norm.startswith(root_norm + os.sep):
            return True
    command_norm = _normalize_path(command)
    if root_norm in command_norm or root in command:
        return True
    return False


def _blocked_command_pattern(command: str) -> Optional[str]:
    for pattern, label in _BLOCKED_COMMAND_PATTERNS:
        if pattern.search(command):
            return label
    return None


def check_repository2_terminal_block(
    command: str,
    effective_workdir: Optional[str] = None,
    *,
    repository2_root: Optional[str] = None,
) -> Optional[str]:
    """Return a block message when a Repository 2 execution command must not run.

    Blocks when the command targets Repository 2 (via the resolved execution
    ``effective_workdir`` or a Repository 2 path embedded in ``command``) and
    matches a blocked execution pattern (pipeline.js, npm scripts, publish,
    preflight, etc.).
    """
    if not command or not str(command).strip():
        return None

    root = repository2_root if repository2_root is not None else get_repository2_root()
    if not _path_touches_repository2(
        command=command,
        effective_workdir=effective_workdir,
        root=root,
    ):
        return None

    if _blocked_command_pattern(command) is None:
        return None

    return REPOSITORY2_BLOCK_MESSAGE


def blocked_command_patterns() -> Sequence[str]:
    """Human-readable labels for tests and diagnostics."""
    return tuple(label for _, label in _BLOCKED_COMMAND_PATTERNS)
