"""COO configuration — safeguard defaults and Execution Engine paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class COOPolicyDefaults:
    """Non-negotiable safeguards inherited from Closed Beta policy."""

    auto_apply: bool = False
    review_required: bool = True
    allow_auto_approval: bool = False
    allow_prompt_mutation: bool = False
    allow_strategy_auto_apply: bool = False
    allow_learning_auto_apply: bool = False
    allow_live_publish: bool = False


@dataclass(frozen=True)
class COOConfig:
    """Runtime configuration for the COO orchestration layer."""

    pipeline_root: Path
    policy: COOPolicyDefaults = COOPolicyDefaults()

    @classmethod
    def from_env(cls) -> "COOConfig":
        root = os.environ.get(
            "CONTENT_PIPELINE_ROOT",
            "/opt/data/multi-content-pipeline",
        )
        return cls(pipeline_root=Path(root))


def get_coo_config() -> COOConfig:
    return COOConfig.from_env()
