"""Production executor policy — Phase 10L safety gate before real Repository2 runs.

Policy evaluation only. No subprocess, no adapter dispatch, no Repository2 mutation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from agent.coo.execution_execute import ExecuteGateStatus
from agent.coo.execution_runtime import ExecutionRunStatus
from agent.coo.skills_catalog import RISK_CATEGORY_PUBLISH, get_skill

if TYPE_CHECKING:
    from agent.coo.execution_dispatch_runtime import (
        DispatchExecutionRequest,
        DispatchUnlockToken,
    )
    from agent.coo.execution_dispatcher import ExecutionDispatchPlan
    from agent.coo.execution_execute import ExecuteGate
    from agent.coo.execution_runtime import ExecutionRun
    from agent.coo.execution_ticket import ExecutionTicket
    from agent.coo.production_executor_confirmation import (
        ProductionExecutorConfirmation,
    )

_REQUIRED_CONFIRMATION_PHRASE = "CONFIRM-REPOSITORY2-EXECUTION"
_NPM_MARKERS = ("npm ", "npm run", "npx ", "npx run")


@dataclass(frozen=True)
class ProductionExecutorPolicy:
    """Operator-controlled policy for enabling real Repository2 dispatch."""

    enabled: bool = False
    requires_explicit_operator_confirmation: bool = True
    allowed_pipeline_roots: tuple[str, ...] = ()
    allowed_entrypoints: tuple[str, ...] = ("node pipeline.js",)
    allow_publish: bool = False
    allow_npm: bool = False
    max_runtime_seconds: int = 3600
    evidence_output_dir: str = "~/.hermes/coo/execution-evidence"
    dry_run_required: bool = True
    execution_review_required: bool = True
    dispatch_token_required: bool = True
    discord_real_execution_allowed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "requires_explicit_operator_confirmation": (
                self.requires_explicit_operator_confirmation
            ),
            "allowed_pipeline_roots": list(self.allowed_pipeline_roots),
            "allowed_entrypoints": list(self.allowed_entrypoints),
            "allow_publish": self.allow_publish,
            "allow_npm": self.allow_npm,
            "max_runtime_seconds": self.max_runtime_seconds,
            "evidence_output_dir": self.evidence_output_dir,
            "dry_run_required": self.dry_run_required,
            "execution_review_required": self.execution_review_required,
            "dispatch_token_required": self.dispatch_token_required,
            "discord_real_execution_allowed": self.discord_real_execution_allowed,
        }


_DEFAULT_POLICY: Optional[ProductionExecutorPolicy] = None


def get_default_production_executor_policy() -> ProductionExecutorPolicy:
    """Return the default production executor policy (disabled)."""
    global _DEFAULT_POLICY
    if _DEFAULT_POLICY is None:
        _DEFAULT_POLICY = ProductionExecutorPolicy()
    return _DEFAULT_POLICY


def _check_item(name: str, passed: bool, reason: str = "") -> Dict[str, Any]:
    return {"name": name, "passed": passed, "reason": reason}


def _normalize_root(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _root_allowed(pipeline_root: str, allowed_roots: tuple[str, ...]) -> tuple[bool, str]:
    if not allowed_roots:
        return False, "allowed_pipeline_roots is empty"
    resolved = _normalize_root(pipeline_root)
    for allowed in allowed_roots:
        allowed_resolved = _normalize_root(allowed)
        try:
            common = os.path.commonpath([resolved, allowed_resolved])
        except ValueError:
            continue
        if common == allowed_resolved:
            return True, ""
    return False, f"pipeline_root {pipeline_root!r} is outside allowed_pipeline_roots"


def _is_publish_skill(skill_id: str) -> bool:
    definition = get_skill(skill_id)
    if definition is None:
        return False
    return definition.risk_category == RISK_CATEGORY_PUBLISH


def _is_npm_entrypoint(entrypoint: str) -> bool:
    normalized = entrypoint.strip().lower()
    return any(normalized.startswith(marker) for marker in _NPM_MARKERS)


def _token_usable_for_policy(token: "DispatchUnlockToken") -> tuple[bool, str]:
    from agent.coo.execution_dispatch_runtime import is_dispatch_unlock_token_expired

    if token.superseded:
        return False, "dispatch unlock token has been superseded"
    if token.consumed:
        return False, "dispatch unlock token has already been consumed"
    if is_dispatch_unlock_token_expired(token):
        return False, "dispatch unlock token has expired"
    return True, ""


def assert_production_executor_allowed(
    policy: ProductionExecutorPolicy,
    *,
    ticket: "ExecutionTicket",
    plan: "ExecutionDispatchPlan",
    dry_run: "ExecutionRun",
    gate: "ExecuteGate",
    token: "DispatchUnlockToken",
    dispatch_request: "DispatchExecutionRequest",
    pipeline_root: str,
    entrypoint: str,
    target_skills: List[str],
    confirmation: Optional["ProductionExecutorConfirmation"] = None,
) -> Dict[str, Any]:
    """Evaluate production executor policy and return a pass/fail checklist.

    Raises ValueError when policy.enabled is False (fail-closed for enabled runs).
    """
    checks: List[Dict[str, Any]] = []

    if not policy.enabled:
        checks.append(
            _check_item("policy_enabled", False, "production executor policy is disabled")
        )
        return _finalize_checklist(checks)

    checks.append(_check_item("policy_enabled", True))

    if policy.dry_run_required:
        dry_ok = (
            dry_run.dry_run is True
            and dry_run.status is ExecutionRunStatus.COMPLETED
            and not dry_run.repository2_touched
        )
        checks.append(
            _check_item(
                "dry_run_completed",
                dry_ok,
                "" if dry_ok else "completed dry-run required before production dispatch",
            )
        )
    else:
        checks.append(_check_item("dry_run_completed", True, "not required"))

    if policy.execution_review_required:
        gate_ok = gate.status is ExecuteGateStatus.APPROVED
        checks.append(
            _check_item(
                "execution_review_approved",
                gate_ok,
                "" if gate_ok else f"execute gate must be approved, got {gate.status.value}",
            )
        )
    else:
        checks.append(_check_item("execution_review_approved", True, "not required"))

    if policy.dispatch_token_required:
        token_ok, token_reason = _token_usable_for_policy(token)
        aligned = dispatch_request.unlock_token_id == token.token_id
        if not aligned:
            token_ok = False
            token_reason = "dispatch_request unlock_token_id does not match token"
        checks.append(
            _check_item(
                "dispatch_token_usable",
                token_ok,
                token_reason,
            )
        )
    else:
        checks.append(_check_item("dispatch_token_usable", True, "not required"))

    root_ok, root_reason = _root_allowed(pipeline_root, policy.allowed_pipeline_roots)
    checks.append(_check_item("pipeline_root_allowed", root_ok, root_reason))

    entry_ok = entrypoint in policy.allowed_entrypoints
    checks.append(
        _check_item(
            "entrypoint_allowed",
            entry_ok,
            "" if entry_ok else f"entrypoint {entrypoint!r} is not in allowed_entrypoints",
        )
    )

    npm_blocked = _is_npm_entrypoint(entrypoint)
    if policy.allow_npm:
        checks.append(_check_item("npm_entrypoint", True, "npm allowed by policy"))
    else:
        checks.append(
            _check_item(
                "npm_entrypoint",
                not npm_blocked,
                "npm entrypoints are not allowed" if npm_blocked else "",
            )
        )

    publish_skills = [skill_id for skill_id in target_skills if _is_publish_skill(skill_id)]
    if policy.allow_publish:
        checks.append(_check_item("publish_targets", True, "publish allowed by policy"))
    else:
        publish_ok = not publish_skills
        checks.append(
            _check_item(
                "publish_targets",
                publish_ok,
                f"publish skills not allowed: {publish_skills}" if publish_skills else "",
            )
        )

    if policy.requires_explicit_operator_confirmation:
        if confirmation is None:
            checks.append(
                _check_item(
                    "operator_confirmation",
                    False,
                    "explicit operator confirmation is required",
                )
            )
        else:
            from agent.coo.production_executor_confirmation import (
                assert_confirmation_valid,
            )

            try:
                assert_confirmation_valid(
                    confirmation,
                    token=token,
                    dispatch_request=dispatch_request,
                    ticket=ticket,
                )
                checks.append(_check_item("operator_confirmation", True))
            except ValueError as exc:
                checks.append(
                    _check_item("operator_confirmation", False, str(exc))
                )
    else:
        checks.append(_check_item("operator_confirmation", True, "not required"))

    if policy.max_runtime_seconds <= 0:
        checks.append(
            _check_item(
                "max_runtime_seconds",
                False,
                "max_runtime_seconds must be positive",
            )
        )
    else:
        checks.append(_check_item("max_runtime_seconds", True))

    evidence_path = Path(os.path.expanduser(policy.evidence_output_dir))
    if str(evidence_path).startswith("/"):
        checks.append(_check_item("evidence_output_dir", True))
    else:
        checks.append(
            _check_item(
                "evidence_output_dir",
                False,
                "evidence_output_dir must be an absolute or home-relative path",
            )
        )

    return _finalize_checklist(checks)


def _finalize_checklist(checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    all_passed = all(item["passed"] for item in checks)
    failed = [item for item in checks if not item["passed"]]
    summary = ""
    if failed:
        summary = "; ".join(
            f"{item['name']}: {item['reason'] or 'failed'}" for item in failed
        )
    return {
        "checks": checks,
        "all_passed": all_passed,
        "summary": summary,
    }
