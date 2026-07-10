"""Shared pipeline root trust checks for COO dispatch CLI (Phase 11A / 11D)."""

from __future__ import annotations

import os
from pathlib import Path

PRODUCTION_ROOT_HARD_DENY = (
    "/opt/data/multi-content-pipeline",
)


def resolve_pipeline_root(pipeline_root: str) -> str:
    """Expand and symlink-resolve a CLI-supplied pipeline root."""
    if not pipeline_root or not str(pipeline_root).strip():
        raise ValueError("pipeline_root is required")
    expanded = Path(os.path.expanduser(pipeline_root.strip()))
    if not expanded.is_absolute():
        raise ValueError("pipeline_root must be an absolute path")
    return os.path.realpath(str(expanded))


def assert_pipeline_root_allowed(resolved: str) -> None:
    """Reject production Repository2 roots and any path inside them."""
    for denied in PRODUCTION_ROOT_HARD_DENY:
        production_root = os.path.realpath(denied)
        try:
            is_inside = os.path.commonpath([resolved, production_root]) == production_root
        except ValueError:
            is_inside = False
        if is_inside:
            raise ValueError("pipeline_root is hard-denied for CLI dispatch")


def assert_pipeline_root_trusted(pipeline_root: str) -> str:
    """Resolve and validate a CLI pipeline root before attestation or execution."""
    resolved = resolve_pipeline_root(pipeline_root)
    assert_pipeline_root_allowed(resolved)
    return resolved


def assert_cli_pipeline_root_trusted(pipeline_root: str) -> str:
    """Resolve and validate CLI pipeline_root before filesystem writes or pre-run checks."""
    return assert_pipeline_root_trusted(pipeline_root)


def assert_pipeline_root_allowed_for_cli(pipeline_root: str) -> None:
    """Reject production Repository2 roots and any path inside them."""
    resolved = os.path.realpath(os.path.expanduser(pipeline_root))
    try:
        assert_pipeline_root_allowed(resolved)
    except ValueError as exc:
        raise ValueError(
            f"pipeline_root {pipeline_root!r} is hard-denied for CLI dispatch run"
        ) from exc


def validate_stored_attested_pipeline_root(attested: str) -> str:
    """Fail-closed validation for attested roots loaded from confirmation files."""
    if not attested or not str(attested).strip():
        raise ValueError("attested_pipeline_root is required")
    stored = Path(str(attested).strip())
    if not stored.is_absolute():
        raise ValueError("attested_pipeline_root must be an absolute path")
    resolved = os.path.realpath(str(stored))
    assert_pipeline_root_allowed(resolved)
    return resolved


def assert_pipeline_root_matches_attestation(
    *,
    cli_pipeline_root: str,
    attested_pipeline_root: str,
) -> str:
    """Require CLI pipeline_root to exactly match the attested confirmation root."""
    cli_resolved = assert_pipeline_root_trusted(cli_pipeline_root)
    stored_resolved = validate_stored_attested_pipeline_root(attested_pipeline_root)
    if cli_resolved != stored_resolved:
        raise ValueError(
            "CLI pipeline_root does not match attested confirmation pipeline root."
        )
    return cli_resolved
