"""COO dispatch executor config loader — Phase 10T scaffold.

Reads ``coo.dispatch.executor`` from merged Hermes config and converts it to
``ProductionExecutorPolicy``. No subprocess, no runner wiring, no config writes.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping

from agent.coo.production_executor_policy import ProductionExecutorPolicy

_PRODUCTION_ROOT_HARD_DENY = (
    "/opt/data/multi-content-pipeline",
)
_KNOWN_EXECUTOR_CONFIG_KEYS = frozenset({"enabled", "allowed_pipeline_roots"})


def _executor_config_section(merged_config: Mapping[str, Any]) -> Dict[str, Any]:
    coo = merged_config.get("coo")
    if coo is None:
        return {}
    if not isinstance(coo, dict):
        raise ValueError("config coo section must be a mapping.")
    dispatch = coo.get("dispatch")
    if dispatch is None:
        return {}
    if not isinstance(dispatch, dict):
        raise ValueError("config coo.dispatch section must be a mapping.")
    executor = dispatch.get("executor")
    if executor is None:
        return {}
    if not isinstance(executor, dict):
        raise ValueError("config coo.dispatch.executor section must be a mapping.")
    return dict(executor)


def _assert_allowlist_path_allowed(path: str) -> None:
    candidate = os.path.realpath(os.path.expanduser(path))
    for denied in _PRODUCTION_ROOT_HARD_DENY:
        production_root = os.path.realpath(denied)
        try:
            is_inside = os.path.commonpath([candidate, production_root]) == production_root
        except ValueError:
            is_inside = False
        if is_inside:
            raise ValueError(
                "allowed_pipeline_roots must not include the production Repository2 root."
            )


def parse_dispatch_executor_config(raw: Any) -> ProductionExecutorPolicy:
    """Parse and validate coo.dispatch.executor config into a policy object."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("coo.dispatch.executor config must be a mapping.")

    unknown_keys = set(raw) - _KNOWN_EXECUTOR_CONFIG_KEYS
    if unknown_keys:
        joined = ", ".join(sorted(unknown_keys))
        raise ValueError(f"Unknown coo.dispatch.executor config keys: {joined}")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("coo.dispatch.executor.enabled must be a boolean.")

    allowlist_raw = raw.get("allowed_pipeline_roots", [])
    if not isinstance(allowlist_raw, list):
        raise ValueError("coo.dispatch.executor.allowed_pipeline_roots must be a list.")
    allowlist: list[str] = []
    for index, entry in enumerate(allowlist_raw):
        if not isinstance(entry, str):
            raise ValueError(
                "coo.dispatch.executor.allowed_pipeline_roots entries must be strings."
            )
        normalized = entry.strip()
        if not normalized:
            raise ValueError(
                "coo.dispatch.executor.allowed_pipeline_roots entries must be non-empty."
            )
        allowlist.append(normalized)

    if enabled and not allowlist:
        raise ValueError(
            "coo.dispatch.executor.enabled=true requires a non-empty allowed_pipeline_roots list."
        )

    for path in allowlist:
        _assert_allowlist_path_allowed(path)

    return ProductionExecutorPolicy(
        enabled=enabled,
        allowed_pipeline_roots=tuple(allowlist),
    )


def load_dispatch_executor_policy(
    merged_config: Mapping[str, Any] | None = None,
) -> ProductionExecutorPolicy:
    """Load dispatch executor policy from merged Hermes config without writing files."""
    if merged_config is None:
        from hermes_cli.config import load_config

        merged_config = load_config()
    if not isinstance(merged_config, dict):
        raise ValueError("Hermes config must be a mapping.")
    return parse_dispatch_executor_config(_executor_config_section(merged_config))
