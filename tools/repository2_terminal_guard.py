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

_ALWAYS_BLOCKED_WITH_UNLOCK_LABELS = frozenset({
    "npm run",
    "npm test",
    "publish",
    "preflight",
})

_PIPELINE_JS_PATTERN = re.compile(r"node\s+pipeline\.js\b", re.I)
_CREATE_CONTENT_SKILL_ID = "create_content"


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


def _command_matches_allowed_entrypoint(command: str, entrypoint: str) -> bool:
    return bool(re.search(rf"{re.escape(entrypoint)}\b", command, re.I))


def _unlock_allows_pipeline_js(
    command: str,
    unlock_context,
    *,
    effective_workdir: Optional[str],
    root: str,
) -> bool:
    if _CREATE_CONTENT_SKILL_ID not in getattr(unlock_context, "target_skills", ()):
        return False
    if not _path_touches_repository2(
        command=command,
        effective_workdir=effective_workdir,
        root=root,
    ):
        return False
    allowed_entrypoints = getattr(unlock_context, "allowed_entrypoints", ())
    if not allowed_entrypoints:
        return False
    if not _PIPELINE_JS_PATTERN.search(command):
        return False
    return any(
        _command_matches_allowed_entrypoint(command, entrypoint)
        for entrypoint in allowed_entrypoints
    )


def check_repository2_terminal_block(
    command: str,
    effective_workdir: Optional[str] = None,
    *,
    repository2_root: Optional[str] = None,
    unlock_context=None,
) -> Optional[str]:
    """Return a block message when a Repository 2 execution command must not run.

    Blocks when the command targets Repository 2 (via the resolved execution
    ``effective_workdir`` or a Repository 2 path embedded in ``command``) and
    matches a blocked execution pattern (pipeline.js, npm scripts, publish,
    preflight, etc.).

    When ``unlock_context`` is active, only ``node pipeline.js`` in the
    Repository 2 workdir is allowed for ``create_content`` targets. Publish,
    preflight, and npm commands remain blocked.
    """
    if not command or not str(command).strip():
        return None

    root = repository2_root if repository2_root is not None else get_repository2_root()
    touches_r2 = _path_touches_repository2(
        command=command,
        effective_workdir=effective_workdir,
        root=root,
    )
    matched_label = _blocked_command_pattern(command)

    if unlock_context is not None:
        if matched_label in _ALWAYS_BLOCKED_WITH_UNLOCK_LABELS and touches_r2:
            return REPOSITORY2_BLOCK_MESSAGE

        if matched_label == "node pipeline.js":
            if not touches_r2:
                return REPOSITORY2_BLOCK_MESSAGE
            if _unlock_allows_pipeline_js(
                command,
                unlock_context,
                effective_workdir=effective_workdir,
                root=root,
            ):
                return None
            return REPOSITORY2_BLOCK_MESSAGE

        if touches_r2 and matched_label is not None:
            return REPOSITORY2_BLOCK_MESSAGE

        return None

    if not touches_r2:
        return None

    if matched_label is not None:
        return REPOSITORY2_BLOCK_MESSAGE

    return None


def blocked_command_patterns() -> Sequence[str]:
    """Human-readable labels for tests and diagnostics."""
    return tuple(label for _, label in _BLOCKED_COMMAND_PATTERNS)
