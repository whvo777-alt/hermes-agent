# Hermes Execution Contract

**Status:** Phase 3D — contract definition only (no execution)  
**Repository:** `/opt/data/Hermes-Agent`  
**Version:** 1.0  
**Last updated:** 2026-07-05

---

## Purpose

Before Worker → Skill → Execution Engine wiring (Phase 4), this document defines
the **Execution Contract**: how Hermes Agent (Repository 1) requests skill
execution against the Execution Engine (Repository 2) without violating
safeguards.

**Non-goals in Phase 3D:** no subprocess, no file writes to Repository 2, no
actual skill dispatch, no gateway changes.

---

## Call Chain

```text
CEO
  → COO (intent, plan, policy)
  → Worker Manager (WorkerAssignment)
  → Worker (plan / perform)
  → SkillExecutionRequest          ← this contract starts here
  → Execution Boundary evaluation
  → SkillExecutionResult
  → Execution Engine entrypoint    ← Phase 4 only; Repository 2
  → ExecutionArtifactRef (read-back)
```

Workers **never** call Repository 2 directly. They produce
`SkillExecutionRequest` records; a future `PipelineAdapter` (Phase 4) evaluates
the boundary policy and dispatches allowed requests.

---

## Worker → Skill Invocation Contract

| Layer | Responsibility |
|-------|----------------|
| **Worker** | Selects which skills to invoke for its staffed phases; builds `SkillExecutionRequest` per skill |
| **Execution Contract** | Validates request against `ExecutionBoundaryPolicy`; returns `SkillExecutionResult` without running |
| **Pipeline Adapter** (Phase 4) | Dispatches allowed EXECUTE requests to Repository 2 entrypoints |
| **Execution Engine** | Runs `pipeline.js`, npm scripts, queue readers — produces artifacts |

### Rules

1. One `SkillExecutionRequest` per skill invocation attempt.
2. Requests carry `worker_id`, `assignment_id`, `skill_id`, `run_date`, and `mode`.
3. Workers attach `SkillInvocation` metadata from COO skill selection; they do not mutate boundary policy.
4. Blocked requests return `SkillExecutionStatus.BLOCKED` — never silently skipped for publish/approval/learning/strategy skills.
5. Repository 2 originals (prompt, strategy, learning, approval scripts) remain read-only from Hermes Agent.

---

## Skill Execution Request Model

```python
SkillExecutionRequest(
    skill_id: str,
    worker_id: str,
    assignment_id: str,
    run_date: str,
    mode: SkillExecutionMode,       # PLAN_ONLY | DRY_RUN | EXECUTE
    phase: PlanPhase | None,
    entrypoint_hint: str,
    parameters: dict,
    auto_apply: bool = False,
    review_required: bool = True,
)
```

| Field | Meaning |
|-------|---------|
| `skill_id` | Catalog skill identifier (e.g. `publish_content`, `create_content`) |
| `worker_id` | Staffing worker that owns this invocation |
| `assignment_id` | Immutable `WorkerAssignment` audit id |
| `run_date` | Pipeline run date (`YYYY-MM-DD`) |
| `mode` | Plan-only, dry-run, or execute intent |
| `phase` | COO plan phase this skill serves |
| `entrypoint_hint` | Repository 2 entrypoint (informational until Phase 4) |
| `parameters` | Structured args for the adapter (never auto-approve flags) |
| `auto_apply` | Must remain `false` |
| `review_required` | Must remain `true` |

---

## Skill Execution Result Model

```python
SkillExecutionResult(
    skill_id: str,
    status: SkillExecutionStatus,
    mode: SkillExecutionMode,
    dry_run: bool,
    summary: str,
    artifacts: list[ExecutionArtifactRef],
    blocked_reason: str,
    auto_apply: bool = False,
    review_required: bool = True,
)
```

| Status | Meaning |
|--------|---------|
| `PLANNED` | Request accepted for planning; no engine call |
| `SKIPPED` | Skill not selected for this worker/phase |
| `BLOCKED` | Boundary policy rejected the request |
| `WAITING` | CEO approval, scheduler, or upstream prerequisite |
| `RUNNING` | Engine dispatch in progress (Phase 4+) |
| `COMPLETED` | Engine finished successfully (Phase 4+) |
| `FAILED` | Engine or adapter error (Phase 4+) |
| `CANCELLED` | CEO terminated (Phase 4+) |

Phase 3D evaluates requests only — results are `PLANNED`, `SKIPPED`, `BLOCKED`, or `WAITING`.

---

## ExecutionArtifactRef

Artifacts are **references only** — never inline blobs.

```python
ExecutionArtifactRef(
    path: str,      # Repository 2 path or logical URI
    kind: str,      # e.g. "report", "queue", "draft"
    summary: str,   # human-readable one-liner
)
```

Hermes Agent reads artifact metadata through the adapter; it does not embed file
contents in COO or Worker results (prompt cache and context budget).

---

## Execution Boundary Policy

Default policy (`ExecutionBoundaryPolicy`) is non-negotiable unless the CEO
explicitly overrides through a future gated config path (not Phase 3D).

| Flag | Default | Effect |
|------|---------|--------|
| `auto_apply` | `false` | Reject requests with `auto_apply=true` |
| `review_required` | `true` | All requests require review boundary |
| `allow_publish` | `false` | Block publish skills until CEO authorizes |
| `allow_approval_decision` | `false` | Block auto-approval / mark-approval skills |
| `allow_learning_apply` | `false` | Block learning mutation skills |
| `allow_strategy_apply` | `false` | Block strategy auto-apply skills |
| `repository2_read_only` | `true` | Hermes never mutates Repository 2 originals |

### Skill category gates

Risk categories are defined on `SkillDefinition.risk_category` in
`agent/coo/skills_catalog.py`. The contract resolves them via
`resolve_risk_category(skill_id)`.

| `risk_category` | Example skill | Gate |
|-------------------|---------------|------|
| `publish` | `publish_content` | `allow_publish` |
| `approval_decision` | (future catalog entries) | `allow_approval_decision` |
| `learning_apply` | (future catalog entries) | `allow_learning_apply` |
| `strategy_apply` | (future catalog entries) | `allow_strategy_apply` |
| `standard` | `create_content`, `daily_brief`, … | No category gate |

Evaluation function: `evaluate_skill_execution(request, boundary)` in
`agent/coo/execution_contract.py` — returns `SkillExecutionResult` with
`BLOCKED` and `blocked_reason` when a gate fails.

---

## Repository 1 vs Repository 2

| | Repository 1 (`Hermes-Agent`) | Repository 2 (`multi-content-pipeline`) |
|--|-------------------------------|----------------------------------------|
| **Role** | Organization, judgment, assignment, contract | Execution, artifacts, queues, runtime |
| **Writes** | WorkerAssignment audit, COO results | Artifacts via engine entrypoints only |
| **Reads** | Pipeline state (read-only inspect) | N/A (engine owns state) |
| **Never touches** | Repository 2 originals | Hermes org models |

```text
┌─────────────────────────────┐         ┌─────────────────────────────┐
│  Repository 1 (Hermes)      │         │  Repository 2 (Engine)      │
│                             │         │                             │
│  COO → Worker → SkillRequest│─evaluate│  pipeline.js, queues,       │
│  ExecutionBoundaryPolicy    │─Phase 4►│  outputs/, _reports/        │
│  SkillExecutionResult       │         │                             │
└─────────────────────────────┘         └─────────────────────────────┘
        read-only inspect ──────────────────────►
        skill dispatch (Phase 4+) ─────────────►
        never mutate originals ◄────────────────X
```

**Out of scope for Hermes modification:** `pipeline.js`, `daily-assignment.js`,
runtime scheduler internals, prompt/strategy/learning/approval originals.

---

## Dry-run vs Execute

### `dry_run` flag semantics

| `dry_run` | Meaning |
|-----------|---------|
| `true` | No actual dispatch occurred — the contract evaluated the request only. |
| `false` | The request **intent** was execute (`SkillExecutionMode.EXECUTE`). This does **not** mean execution completed. In Phase 3D, allowed EXECUTE requests still return `status=PLANNED` because adapter dispatch is deferred to Phase 4. |

### Mode behavior

| Mode | Phase 3D behavior | Phase 4A behavior | Phase 4B+ (future) |
|------|-------------------|-------------------|---------------------|
| `PLAN_ONLY` | Evaluate boundary; status `PLANNED`, `dry_run=true` | Same — no engine call | Same |
| `DRY_RUN` | Evaluate boundary; status `PLANNED`, `dry_run=true` | Contract + `PipelineAdapter.dry_run()`; validate root, no subprocess | Validate entrypoint + args; optional subprocess |
| `EXECUTE` | Evaluate boundary only — **no dispatch**; `dry_run=false`, `status=PLANNED` | `dispatch()` raises `RuntimeError` | Adapter invokes entrypoint if gates pass |

### Worker → Skill mode compatibility

Skill execution mode must not be **stronger** than the worker's execution mode:

| Worker mode | Allowed skill modes |
|-------------|---------------------|
| `PLAN_ONLY` | `PLAN_ONLY` only |
| `DRY_RUN` | `PLAN_ONLY`, `DRY_RUN` |
| `EXECUTE` | `PLAN_ONLY`, `DRY_RUN`, `EXECUTE` |

`WorkerManager.dry_run()` rejects `WorkerExecutionMode.EXECUTE` until Phase 4.
Skill-level `SkillExecutionMode.EXECUTE` is evaluated by the contract layer
but never dispatched in Phase 3D.

### Risk classification source of truth

Skill risk categories (`publish`, `approval_decision`, `learning_apply`,
`strategy_apply`, `standard`) are defined on `SkillDefinition` in
`agent/coo/skills_catalog.py`. The execution contract reads catalog metadata
via `resolve_risk_category()` — not hardcoded skill ID lists.

---

## Pipeline Adapter Boundary (Phase 4A)

The **Pipeline Adapter** (`agent/coo/pipeline_adapter.py`) is the **only** Hermes
layer that knows Repository 2 filesystem paths and shell entrypoint hints.

| Rule | Detail |
|------|--------|
| Workers | Must **not** reference Repository 2 paths or `node`/`npm` commands |
| Adapter | Resolves `SkillDefinition.entrypoint_hint` from catalog **only** |
| Request override | `SkillExecutionRequest.entrypoint_hint` is **ignored**; mismatch logs a warning |
| Phase 4A scope | **DRY_RUN only** — plan + validate root; no subprocess |
| `allow_execute` | Default `false`; must remain `false` in Phase 4A |
| Subprocess | **Forbidden** in Phase 4A — no `node`, no `npm`, no shell dispatch |
| Repository 2 | **Read-only** from Hermes; `_assert_repository_read_only()` fails closed if disabled |

### Request flow (Phase 4A)

```
SkillExecutionRequest
        │
        ▼
evaluate_skill_execution()     ← boundary + mode gates (Phase 3D contract)
        │
        ├── BLOCKED ──► SkillExecutionResult (adapter not reached)
        │
        ▼
PipelineAdapter.dry_run()      ← validate_root + entrypoint metadata only
        │
        ▼
SkillExecutionResult           ← PLANNED (dry_run=true) or FAILED
```

`PipelineAdapter.dispatch()` evaluates the contract first, then raises
`RuntimeError` in Phase 4A — actual engine invocation is deferred to Phase 4B+.

### Adapter config defaults

| Field | Default | Notes |
|-------|---------|-------|
| `pipeline_root` | `/opt/data/multi-content-pipeline` | Existence check only in 4A |
| `allow_execute` | `false` | Must stay false until explicit Phase 4B gate |
| `timeout_seconds` | `60` | Reserved for future dispatch |
| `repository2_read_only` | `true` | Hermes never writes R2 originals |

---

## Safeguards (Non-Negotiable)

| Safeguard | Enforcement |
|-----------|-------------|
| `autoApply=false` | Request default + boundary policy + result echo |
| `reviewRequired=true` | Request default + boundary policy + result echo |
| CEO approval before publish | `allow_publish=false` blocks publish skills |
| No auto-approval | `allow_approval_decision=false` |
| No learning auto-apply | `allow_learning_apply=false` |
| No strategy auto-apply | `allow_strategy_apply=false` |
| Repository 2 read-only | `repository2_read_only=true`; adapter is sole write path |

---

## Phase Roadmap

| Phase | Deliverable |
|-------|-------------|
| **3D** | `execution_contract.py` models + boundary evaluation |
| **4A** (now) | `pipeline_adapter.py` — DRY_RUN plan/validate; no subprocess |
| **4B+** | `PipelineAdapter.dispatch()` with gated execute + RUNNING/COMPLETED |
| **5** | Discord CEO → Worker → Skill → Engine end-to-end |

---

## Related Documents

- `docs/HERMES_ORGANIZATION_ARCHITECTURE.md` — Worker Organization v1.1
- `agent/coo/worker_interface.py` — Worker plan/perform contract
- `agent/coo/skills_catalog.py` — COO-facing skill metadata
- `agent/coo/execution_contract.py` — skill request/result models (Phase 3D)
- `agent/coo/pipeline_adapter.py` — Repository 2 adapter (Phase 4A)
