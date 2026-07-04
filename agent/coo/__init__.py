"""Hermes COO — orchestration layer above the Execution Engine.

Hermes acts as the AI company's COO: it interprets CEO intent, plans work,
evaluates policy against current pipeline state, and selects Execution Engine
skills. Repository 2 (multi-content-pipeline) remains the Execution Engine;
this package never modifies pipeline.js, runtime, learning, prompt, strategy,
or approval originals.
"""

from agent.coo.models import (
    COOOrchestrationResult,
    ExecutionPlan,
    IntentResult,
    PipelineState,
    PolicyDecision,
    SkillInvocation,
    TaskKind,
)
from agent.coo.orchestrator import COOOrchestrator

__all__ = [
    "COOOrchestrator",
    "COOOrchestrationResult",
    "ExecutionPlan",
    "IntentResult",
    "PipelineState",
    "PolicyDecision",
    "SkillInvocation",
    "TaskKind",
]
