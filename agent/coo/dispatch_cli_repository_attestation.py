"""CLI dispatch repository read-only attestation — Phase 12T.

Stat/read/hash verification of Repository2 production identity and structure.
No subprocess, writes, node/npm/git execution, or content disclosure.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from agent.coo.dispatch_pipeline_root_trust import (
    PRODUCTION_ROOT_HARD_DENY,
    assert_pipeline_root_allowed,
)

RECOMMENDED_NEXT_PHASE = "Phase 12U Production Dispatch Sign-off"

EXPECTED_REPOSITORY2_PRODUCTION_ROOT = PRODUCTION_ROOT_HARD_DENY[0]

REQUIRED_ENTRYPOINT = "pipeline.js"
REQUIRED_MANIFEST = "package.json"
REQUIRED_DIRECTORIES = ("publishers", "prompts", "config")
OPTIONAL_DIRECTORIES = ("outputs", "reports")

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class _FileStatSnapshot:
    st_ino: int
    st_size: int
    st_mtime_ns: int
    st_mode: int


@dataclass(frozen=True)
class CooDispatchRepositoryAttestationSummary:
    """Safe read-only Repository2 attestation summary."""

    repository_attested: bool
    root_matches_expected: bool
    root_is_symlink: bool
    pipeline_entrypoint_present: bool
    package_manifest_present: bool
    required_directories_present_count: int
    required_directories_missing: str
    optional_directories_present: str
    pipeline_sha256: str
    package_sha256: str
    git_metadata_present: bool
    git_head_kind: str
    git_head_value: str
    execution_allowed: bool
    production_root_hard_deny: bool
    recommended_next_phase: str


def _snapshot_stat(path: Path) -> _FileStatSnapshot:
    stat = path.stat()
    return _FileStatSnapshot(
        st_ino=stat.st_ino,
        st_size=stat.st_size,
        st_mtime_ns=stat.st_mtime_ns,
        st_mode=stat.st_mode,
    )


def _assert_stat_unchanged(before: _FileStatSnapshot, after: _FileStatSnapshot) -> None:
    if before != after:
        raise ValueError("file metadata changed during attestation")


def _resolve_attestation_root(repository_root: str) -> tuple[Path, bool]:
    if not repository_root or not str(repository_root).strip():
        raise ValueError("repository_root is required")
    expanded = Path(os.path.expanduser(repository_root.strip()))
    if not expanded.is_absolute():
        raise ValueError("repository_root must be an absolute path")
    if any(part == ".." for part in expanded.parts):
        raise ValueError("repository_root must not contain path traversal")
    root_is_symlink = expanded.is_symlink()
    if root_is_symlink:
        raise ValueError("repository_root must not be a symlink")
    resolved = Path(os.path.realpath(str(expanded)))
    expected = Path(os.path.realpath(EXPECTED_REPOSITORY2_PRODUCTION_ROOT))
    if resolved != expected:
        raise ValueError("repository_root does not match expected production root")
    if not resolved.is_dir():
        raise ValueError("repository_root is missing or not a directory")
    return resolved, root_is_symlink


def _assert_regular_readable_file(path: Path, label: str) -> None:
    if not path.exists():
        raise ValueError(f"{label} is missing")
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    if not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise ValueError(f"{label} is not readable") from exc


def _sha256_file_with_toctou(path: Path) -> str:
    before = _snapshot_stat(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    after = _snapshot_stat(path)
    _assert_stat_unchanged(before, after)
    return digest


def _validate_package_manifest(path: Path) -> None:
    _assert_regular_readable_file(path, REQUIRED_MANIFEST)
    before = _snapshot_stat(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("package.json is corrupted or unreadable") from exc
    after = _snapshot_stat(path)
    _assert_stat_unchanged(before, after)
    if not isinstance(data, dict):
        raise ValueError("package.json is corrupted or unreadable")
    if "scripts" not in data or not isinstance(data.get("scripts"), dict):
        raise ValueError("package.json missing required scripts key")


def _collect_required_directories(resolved: Path) -> tuple[int, list[str]]:
    present = 0
    missing: list[str] = []
    for name in REQUIRED_DIRECTORIES:
        dir_path = resolved / name
        if dir_path.is_symlink() or not dir_path.is_dir():
            missing.append(name)
            continue
        present += 1
    return present, missing


def _collect_optional_directories(resolved: Path) -> list[str]:
    present: list[str] = []
    for name in OPTIONAL_DIRECTORIES:
        dir_path = resolved / name
        if dir_path.is_symlink():
            continue
        if dir_path.is_dir():
            present.append(name)
    return present


def _read_git_metadata(root: Path) -> tuple[bool, str, str]:
    git_dir = root / ".git"
    if not git_dir.exists():
        return False, "", ""
    head_path = git_dir / "HEAD"
    if not head_path.is_file() or head_path.is_symlink():
        return True, "unknown", _NONE_LABEL
    content = head_path.read_text(encoding="utf-8", errors="strict").strip()
    if content.startswith("ref: "):
        ref = content[5:].strip()
        ref_path = git_dir / ref
        ref_name = ref.rsplit("/", 1)[-1]
        if ref_path.is_file() and not ref_path.is_symlink():
            commit = ref_path.read_text(encoding="ascii", errors="replace").strip()
            short = commit[:12] if len(commit) >= 7 else commit
            return True, "symbolic_ref", f"{ref_name}@{short}"
        return True, "symbolic_ref", ref_name
    hex_chars = set("0123456789abcdefABCDEF")
    if len(content) >= 7 and all(char in hex_chars for char in content[:12]):
        return True, "commit", content[:12]
    return True, "unknown", _NONE_LABEL


def _verify_execution_still_denied() -> None:
    """Confirm official production roots remain hard-denied for dispatch execution."""
    for denied in PRODUCTION_ROOT_HARD_DENY:
        production_root = os.path.realpath(denied)
        try:
            assert_pipeline_root_allowed(production_root)
        except ValueError:
            continue
        raise ValueError("production root hard-deny is not active")


def attest_repository2_production_root(
    *,
    repository_root: str,
) -> CooDispatchRepositoryAttestationSummary:
    """Read-only attestation of the official Repository2 production root."""
    resolved, root_is_symlink = _resolve_attestation_root(repository_root)
    root_matches = str(resolved) == os.path.realpath(
        EXPECTED_REPOSITORY2_PRODUCTION_ROOT
    )

    pipeline_path = resolved / REQUIRED_ENTRYPOINT
    package_path = resolved / REQUIRED_MANIFEST

    _assert_regular_readable_file(pipeline_path, REQUIRED_ENTRYPOINT)
    _validate_package_manifest(package_path)

    required_present, required_missing = _collect_required_directories(resolved)
    if required_missing:
        raise ValueError("required repository directories are missing")

    optional_present = _collect_optional_directories(resolved)
    pipeline_sha = _sha256_file_with_toctou(pipeline_path)
    package_sha = _sha256_file_with_toctou(package_path)
    git_present, git_kind, git_value = _read_git_metadata(resolved)
    _verify_execution_still_denied()

    return CooDispatchRepositoryAttestationSummary(
        repository_attested=True,
        root_matches_expected=root_matches,
        root_is_symlink=root_is_symlink,
        pipeline_entrypoint_present=True,
        package_manifest_present=True,
        required_directories_present_count=required_present,
        required_directories_missing=_NONE_LABEL,
        optional_directories_present=(
            ",".join(optional_present) if optional_present else _NONE_LABEL
        ),
        pipeline_sha256=pipeline_sha,
        package_sha256=package_sha,
        git_metadata_present=git_present,
        git_head_kind=git_kind,
        git_head_value=git_value,
        execution_allowed=False,
        production_root_hard_deny=True,
        recommended_next_phase=RECOMMENDED_NEXT_PHASE,
    )


def format_dispatch_repository_attestation(
    summary: CooDispatchRepositoryAttestationSummary,
) -> str:
    """Format safe attestation fields for CLI stdout."""
    lines = [
        "Repository Attestation",
        "",
        f"repository_attested: {str(summary.repository_attested).lower()}",
        f"root_matches_expected: {str(summary.root_matches_expected).lower()}",
        f"root_is_symlink: {str(summary.root_is_symlink).lower()}",
        (
            "pipeline_entrypoint_present: "
            f"{str(summary.pipeline_entrypoint_present).lower()}"
        ),
        (
            "package_manifest_present: "
            f"{str(summary.package_manifest_present).lower()}"
        ),
        (
            "required_directories_present_count: "
            f"{summary.required_directories_present_count}"
        ),
        f"required_directories_missing: {summary.required_directories_missing}",
        f"optional_directories_present: {summary.optional_directories_present}",
        f"pipeline_sha256: {summary.pipeline_sha256}",
        f"package_sha256: {summary.package_sha256}",
        (
            "git_metadata_present: "
            f"{str(summary.git_metadata_present).lower()}"
        ),
        f"git_head_kind: {summary.git_head_kind or _NONE_LABEL}",
        f"git_head_value: {summary.git_head_value or _NONE_LABEL}",
        f"execution_allowed: {str(summary.execution_allowed).lower()}",
        (
            "production_root_hard_deny: "
            f"{str(summary.production_root_hard_deny).lower()}"
        ),
        f"recommended_next_phase: {summary.recommended_next_phase}",
    ]
    return "\n".join(lines)
