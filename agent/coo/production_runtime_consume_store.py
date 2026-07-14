"""Shared one-shot consume-record write helper — Phase 15I.

Generic O_CREAT|O_EXCL JSON artifact writer reused by the permission,
boundary, invocation, and authorization consume stores. This module never
mutates an existing artifact and never touches subprocess, Repository2, or
any bounded runner. Path validation always happens before any filesystem
mutation (mkdir/write), consistent with AGENTS.md's "validate before
mutate" policy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home


class OneShotConsumeWriteConflict(ValueError):
    """Raised when a consume record already exists at the target path."""


def assert_consume_path_under_hermes_home(path: Path) -> Path:
    """Fail closed when a consume-record path would escape Hermes home."""
    hermes_root = get_hermes_home().resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ValueError(
            f"Consume record path {resolved} must remain under Hermes home {hermes_root}"
        ) from exc
    return resolved


def write_once_consume_record(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write a JSON consume record exactly once.

    Raises OneShotConsumeWriteConflict if a record already exists at path.
    The underlying os.O_CREAT|os.O_EXCL open is the actual one-shot
    enforcement mechanism (safe under concurrent callers), not a
    check-then-write race.
    """
    resolved = assert_consume_path_under_hermes_home(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(dict(payload), indent=2, sort_keys=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(resolved), flags, 0o644)
    except FileExistsError as exc:
        raise OneShotConsumeWriteConflict(
            f"Consume record already exists: {resolved}"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            resolved.unlink()
        except OSError:
            pass
        raise


def read_consume_record(path: Path) -> dict[str, Any] | None:
    """Read a consume record if present; None if never consumed."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Consume record corrupted: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Consume record must be a JSON object: {path}")
    return payload
