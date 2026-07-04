# Hermes Organization Architecture v1

**Status:** Phase 3A — architecture document only (no implementation)  
**Repository:** `/opt/data/Hermes-Agent`  
**Version:** 1.1 (Claude Architecture Review revision)  
**Last updated:** 2026-07-05

---

## Purpose

Hermes is not a flat **Agent → Skill** stack. It is an **AI company** with
organizational roles, departments, workers, and an execution engine.

This document defines **Hermes Organization Architecture v1**: how the CEO
(user), COO (Hermes orchestration layer), Worker Manager, Departments, and
Workers relate to Execution Engine skills and Repository 2
(`multi-content-pipeline`).

**Non-goals in Phase 3A:** no gateway changes, no `run_agent.py` changes, no
Repository 2 changes, no automatic approval or publishing.

---

## Core Safeguards (Non-Negotiable)

| Safeguard | Value | Scope |
|-----------|-------|-------|
| `autoApply` | always `false` | COO, Worker Manager, all Workers |
| `reviewRequired` | always `true` | COO, Worker Manager, all Workers |
| Auto-approval | forbidden | Approval Department |
| Auto-publish | forbidden | Publishing Department |
| Learning auto-apply | forbidden | Learning Department |
| Strategy auto-apply | forbidden | Strategy Department |
| Repository 2 originals | read-only | prompt, strategy, learning, approval originals |

Hermes Agent owns **judgment and assignment**. Repository 2 owns **execution
artifacts and queues**.

---

## 1. Organization Chart

```text
                              CEO
                               │
                               │  natural-language intent
                               │  approval / rejection decisions
                               ▼
                         Hermes COO
                               │
           ┌───────────────────┼───────────────────┐
           │                   │                   │
     Intent Analysis    Execution Planner   Execution Policy
           │                   │                   │
           └───────────────────┼───────────────────┘
                               │
                               │  task plan + policy verdict
                               │  (no direct worker/skill knowledge)
                               ▼
                        Worker Manager
                               │
       ┌───────────┬───────────┼───────────┬───────────┐
       │           │           │           │           │
       ▼           ▼           ▼           ▼           ▼
  Research    Strategy     Writing     Quality    Approval
 Department  Department  Department  Department  Department
       │           │           │           │           │
       ▼           ▼           ▼           ▼           ▼
   Workers      Workers     Workers     Workers     Workers
       │           │           │           │           │
       └───────────┴───────────┴───────────┴───────────┘
                               │
       ┌───────────┬───────────┼───────────┬───────────┐
       │           │           │           │           │
       ▼           ▼           ▼           ▼           ▼
 Publishing   Learning    Reporting    (future
 Department  Department  Department    departments)
       │           │           │
       ▼           ▼           ▼
   Workers      Workers     Workers
       │           │           │
       └───────────┴───────────┴───────────────────────┘
                               │
                               │  skill dispatch (Phase 4+)
                               ▼
                    Execution Engine (Repository 2)
                               │
                               ▼
              outputs / queues / reports / runtime state
```

### Role Summary

| Role | Entity | Responsibility |
|------|--------|----------------|
| **CEO** | Human user | Sets goals, approves content, rejects risk, authorizes publish |
| **COO** | `COOOrchestrator` | Intent → plan → policy; delegates to Worker Manager |
| **Worker Manager** | Future `WorkerManager` | Selects departments/workers, tracks lifecycle, aggregates status |
| **Department** | Organizational unit | Groups related workers under one functional area |
| **Worker** | AI employee | Owns a portfolio of skills; executes role-specific work |
| **Skill** | Tool / entrypoint | Atomic capability a worker invokes against Repository 2 |
| **Execution Engine** | Repository 2 | Runs pipeline phases, stores artifacts, manages queues |

---

## 2. Worker Manager

The Worker Manager sits **between COO and Workers**. It is the middle
management layer that keeps COO decoupled from individual worker identities.

### 2.1 Responsibilities

| Function | Description |
|----------|-------------|
| **Worker selection** | Map COO `ExecutionPlan` phases to department workers |
| **Assignment** | Produce `WorkerAssignment` records with lifecycle state |
| **Status collection** | Aggregate worker states into a COO-facing summary |
| **Boundary enforcement** | Apply policy verdicts at worker level before skill planning |
| **Abstraction** | COO sees departments + assignments, not skill entrypoints |

### 2.2 Why COO Must Not Know Individual Workers

COO's job is **executive planning**:

- What task is the CEO asking for?
- What phases are allowed, blocked, or deferred?
- What safeguards apply?

Worker Manager's job is **operational staffing**:

- Which department owns this phase?
- Which worker within the department should run?
- What is each worker's current lifecycle state?

This separation preserves:

1. **Stable COO interface** — plan/policy models do not grow with every new worker
2. **Department scalability** — new workers register under departments without COO changes
3. **Testability** — Worker Manager can be mocked independently of policy logic
4. **Prompt cache safety** — COO system surface stays narrow; worker detail lives in tool results

### 2.3 Worker Manager Interface (proposed, Phase 3B)

```text
Input:  ExecutionPlan + PolicyDecision + PipelineState
Output: WorkerOrganizationResult
          ├── department_assignments[]
          ├── worker_assignments[]
          ├── aggregate_status
          └── ceo_summary
```

COO calls Worker Manager **after** policy evaluation and **before** any skill
dispatch. COO never calls skills directly.

---

## 3. Department Structure

Each **Department** is a functional unit containing one or more **Workers**.
Departments map to `PlanPhase` groups in the COO execution plan.

### 3.1 Research Department

**Mission:** Gather external and internal signals before strategy decisions.

| Worker | Primary Skills (tools) |
|--------|------------------------|
| Trend Worker | Trend analysis, topic momentum |
| Keyword Worker | Keyword research, search intent |
| News Worker | News digest, current events scan |
| Competitor Worker | Competitor content analysis |

**Repository 2 touch:** read `outputs/<date>/research/*`  
**Write policy:** read-only in Phase 3–4; mutations via scoped skills only (Phase 4+)

### 3.2 Strategy Department

> **Naming note:** "Strategy Department" here means **Editorial Strategy**
> (topic selection, content direction, calendar). It is a different concept
> from Repository 2's **Strategy Observer** (the Execution Engine's runtime
> strategy component). The department name is kept for organizational
> clarity, but readers and future implementers must not conflate the two —
> this department never touches the Strategy Observer or strategy originals.

**Mission:** Turn research into editorial direction without auto-applying strategy.

| Worker | Primary Skills |
|--------|----------------|
| Topic Selection Worker | Topic shortlist, angle selection |
| Strategy Brief Worker | Strategy document drafting (proposal only) |
| Calendar Worker | Content calendar alignment |

**Repository 2 touch:** read strategy artifacts  
**Safeguard:** strategy auto-apply forbidden — CEO review required

### 3.3 Writing Department

**Mission:** Produce draft content and supporting assets.

| Worker | Primary Skills |
|--------|----------------|
| Draft Worker | Long-form draft generation |
| SEO Worker | Title, meta, keyword placement |
| Formatting Worker | Markdown/HTML normalization |
| Image Planning Worker | Image slot planning (no auto-generation) |

**Repository 2 touch:** read/write draft artifacts via skill entrypoints only

### 3.4 Quality Department

**Mission:** Validate content quality before approval queue.

| Worker | Primary Skills |
|--------|----------------|
| Quality Check Worker | Grammar, structure, brand voice |
| Fact Check Worker | Claim verification (read-only sources) |
| Verify Worker | Runtime smoke / closed-beta verification chain |

**Repository 2 touch:** read QA reports, verification outputs

### 3.5 Approval Department

**Mission:** Prepare approval surfaces and CEO decision support.

| Worker | Primary Skills |
|--------|----------------|
| Approval Queue Worker | Queue inspection, pending item summary |
| Risk Check Worker | Policy/risk flag review |
| CEO Report Worker | Formatted CEO briefing |
| Approval Decision Worker | Record CEO decision (CEO-triggered only) |

**Repository 2 touch:** read approval queue  
**Safeguard:** auto-approval forbidden — `Approval Decision Worker` runs only
after explicit CEO instruction

### 3.6 Publishing Department

**Mission:** Post-approval dispatch with scheduler awareness.

| Worker | Primary Skills |
|--------|----------------|
| Preflight Worker | Publish readiness checks |
| Publisher Worker | Dispatch orchestration |
| Scheduler Worker | Scheduler runtime coordination |

**Repository 2 touch:** publishing plan, confirmed queue, scheduler state  
**Safeguard:** auto-publish forbidden; defer when scheduler owns dispatch

### 3.7 Learning Department

**Mission:** Capture and propose learnings without auto-applying them.

| Worker | Primary Skills |
|--------|----------------|
| Learning Review Worker | Read learning proposals |
| Pattern Worker | Identify recurring patterns |
| Feedback Worker | Ingest CEO/post-run feedback |

**Repository 2 touch:** read learning sources (never mutate originals)  
**Safeguard:** learning auto-apply forbidden

### 3.8 Reporting Department

**Mission:** Operational visibility for the CEO.

| Worker | Primary Skills |
|--------|----------------|
| Daily Brief Worker | Read-only daily status report |
| Pipeline Status Worker | Phase completion summary |
| Incident Worker | Blocker/deferral explanation |

**Repository 2 touch:** read `_reports/*`, run reports — read-only

### 3.9 Department → Phase Mapping

| Department | COO PlanPhase(s) |
|------------|------------------|
| Research | `research` |
| Strategy | `strategy` |
| Writing | `writer` |
| Quality | `quality` |
| Approval | `approval_queue`, `approval_check`, `ceo_report` |
| Publishing | `publisher`, `publish_wait` |
| Learning | post-run (no default phase — CEO-gated) |
| Reporting | `ceo_report` (read-only briefs) |

---

## 4. Worker Lifecycle

Every worker assignment progresses through a defined lifecycle. Lifecycle
state is owned by **Worker Manager**, not COO.

### 4.1 States

```text
PLANNED ──► SELECTED ──► WORKING ──► COMPLETED
                │            │
                │            ├──► WAITING ──► WORKING (resume)
                │            │
                ▼            ▼
             BLOCKED       FAILED

Any non-terminal state ──► CANCELLED  (explicit CEO termination)
```

| State | Meaning | Terminal? |
|-------|---------|-----------|
| `PLANNED` | Worker identified for a phase but not yet policy-checked | No |
| `SELECTED` | Policy allows; worker is staffed for the assignment | No |
| `WORKING` | Worker is actively invoking skills or producing output | No |
| `WAITING` | Paused for external input — **not a failure** | No |
| `BLOCKED` | Policy or safeguard prevents execution | Yes |
| `COMPLETED` | Worker finished its assignment successfully | Yes |
| `FAILED` | Skill dispatch or unrecoverable error | Yes |
| `CANCELLED` | CEO explicitly terminated the assignment | Yes |

### 4.1.1 CANCELLED Semantics

`CANCELLED` is the state in which the **CEO explicitly ends an assignment**.
It is distinct from the other terminal states:

| State | Cause | Initiated by |
|-------|-------|--------------|
| `BLOCKED` | Policy or safeguard prevents execution | Policy engine |
| `FAILED` | Skill dispatch or unrecoverable error | Execution failure |
| `CANCELLED` | Explicit termination decision | **CEO** |

A `CANCELLED` assignment is not an error and not a policy violation — it is a
recorded executive decision. Worker Manager reports it as such in the CEO
summary, and downstream workers that depended on the cancelled assignment
move to `WAITING` or are themselves cancelled by the CEO.

### 4.2 WAITING Semantics

`WAITING` is a **non-failure pause**. Common causes:

| Wait Reason | Example | Resumed By |
|-------------|---------|------------|
| CEO approval pending | Content in approval queue | CEO approves/rejects |
| Scheduler ownership | Scheduler runtime active | CEO manual override or scheduler completion |
| Upstream worker | Strategy waiting on Research artifacts | Prior worker completes |
| External result | Async skill dispatch (Phase 4+) | Callback / poll |

Worker Manager aggregates `WAITING` workers into the COO CEO summary without
marking the overall task as failed.

### 4.3 Lifecycle Ownership

```text
COO          → sets plan + policy (what may run)
Worker Manager → assigns workers + tracks lifecycle
Worker       → executes skills within assignment
CEO          → resolves WAITING at approval boundaries
             → may CANCEL any non-terminal assignment
```

### 4.4 Recovery Policy

**BLOCKED assignments are never resumed.**

| Rule | Detail |
|------|--------|
| No resume | A `BLOCKED` assignment does not transition back to `SELECTED` or `WORKING` |
| Immutable record | A `BLOCKED` assignment is an immutable audit record of the policy decision that blocked it |
| New assignment on policy change | When policy or pipeline state changes (e.g. CEO approves queue items), **COO re-runs orchestration and Worker Manager creates a NEW `WorkerAssignment`** with a new assignment id |
| History preserved | The old `BLOCKED` assignment remains in the assignment history for CEO auditability |

Rationale: resuming a `BLOCKED` assignment would mean mutating a record whose
policy context no longer matches reality. A fresh assignment re-evaluates
intent → plan → policy against the **current** pipeline state, so the
safeguard chain is never bypassed by a stale resume.

The same immutability applies to `FAILED` and `CANCELLED`: terminal states
are final for that assignment; recovery always flows through a new COO
orchestration pass.

---

## 5. Worker vs Skill

| Dimension | Worker | Skill |
|-----------|--------|-------|
| **Metaphor** | AI employee | Tool the employee uses |
| **Scope** | Role + portfolio + lifecycle | Single entrypoint + I/O contract |
| **Cardinality** | 1 Worker : N Skills | 1 Skill : 1 entrypoint |
| **Owned by** | Hermes Agent (`agent/coo/workers/`) | Skill catalog + Repository 2 |
| **Selected by** | Worker Manager | Worker (not COO, not CEO directly) |
| **Lifecycle** | PLANNED → … → COMPLETED | invoked / skipped per assignment |
| **Policy** | Department + worker safeguards | Entrypoint-level read/write rules |

### 5.1 Call Chain (Target State)

```text
CEO message
  → COO (intent, plan, policy)
  → Worker Manager (select workers, assign, track)
  → Worker (plan + invoke skills)
  → Skill (Repository 2 entrypoint)
  → Artifact / queue / report
```

**COO never calls a skill directly.**  
**Worker Manager never executes a skill directly** (Phase 3B — planning only;
Phase 4 — delegates execution to Worker).

### 5.2 Example: Writing Department

```text
Writing Department
  └── Draft Worker                    ← AI employee
        ├── draft_skill               ← node/scoped writer entrypoint
        ├── seo_skill                 ← metadata validation
        ├── formatting_skill          ← output normalization
        └── image_planning_skill      ← slot planning
```

The CEO says "오늘 블로그 글 작성해서 보고해". COO produces a
`create_and_report` plan. Worker Manager staffs Research → Strategy → Writing
→ Quality → Approval → Reporting workers in order. Each worker selects its
own skills within policy bounds.

---

## 6. Company Memory

**Company Memory** is the long-term organizational knowledge layer that will
connect to the Learning Engine. It is distinct from:

- **Session memory** (Hermes `memory` tool / memory providers — per-user, per-profile)
- **WorkerContext** (turn-local execution snapshot)
- **Repository 2 learning sources** (execution engine artifacts — read-only originals)

### 6.1 Purpose

| Layer | Horizon | Examples |
|-------|---------|----------|
| Session memory | Current conversation | User preferences, prior turn facts |
| WorkerContext | Current assignment | Plan, policy, prior worker results |
| Company Memory | Cross-run, cross-worker | Editorial patterns, CEO decision history, department playbooks |

### 6.2 Future Integration (not Phase 3A)

```text
Company Memory
  ├── editorial_patterns      (what worked, what CEO rejected)
  ├── department_playbooks  (Research/Writing procedures)
  ├── ceo_decision_log      (approval history — read by Approval Dept)
  └── learning_proposals    (from Learning Engine — propose only, never auto-apply)
```

`WorkerContext` **may include** a `company_memory` field in future phases:

```python
@dataclass
class WorkerContext:
    ...
    company_memory: CompanyMemorySnapshot | None = None  # Phase 4+
```

**Phase 3A:** document the concept only. No schema, no storage, no provider.

### 6.3 Safeguards

Company Memory must not bypass safeguards:

- Stored learnings are **proposals** until CEO approves
- Company Memory does not mutate Repository 2 originals
- Company Memory writes go through Learning Department workers (future)

---

## 7. Repository Boundary

### 7.1 Two-Repository Model

| Repository | Path | Role |
|------------|------|------|
| **Hermes Agent** | `/opt/data/Hermes-Agent` | Organization, judgment, assignment, COO, Worker Manager, Workers |
| **Execution Engine** | `/opt/data/multi-content-pipeline` | Pipeline execution, artifacts, approval queues, publishing, runtime |

```text
┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│  Hermes Agent (Organization)    │     │  Repository 2 (Execution)       │
│                                 │     │                                 │
│  CEO → COO → Worker Manager     │     │  pipeline.js, runtime, queues   │
│       → Workers → Skills ───────┼────►│  outputs/, _reports/, learning/ │
│                                 │     │                                 │
│  Judgment & assignment          │     │  Execution & artifacts          │
└─────────────────────────────────┘     └─────────────────────────────────┘
         read-only inspect ──────────────────────►
         skill dispatch (Phase 4+) ─────────────►
         never mutate originals ◄────────────────X
```

### 7.2 Read vs Write

| Action | Hermes Agent | Repository 2 |
|--------|--------------|----------------|
| Intent analysis | yes | — |
| Policy evaluation | yes (read state) | read state files |
| Worker assignment | yes | — |
| Skill execution | via Worker dispatch | executes |
| Artifact creation | — | via skill entrypoints |
| Approval queue mutation | **never** (CEO only, via explicit action) | queue files |
| Prompt/strategy/learning originals | **never** | owned by Engine |

### 7.3 Files Explicitly Out of Scope for Hermes Modification

- `pipeline.js`
- `daily-assignment.js`
- Runtime scheduler internals
- Learning source originals
- Strategy originals
- Approval original scripts (`mark:approval`, auto-approval paths)

Hermes reads these surfaces through **PipelineStateReader** and future
**PipelineAdapter** — never edits them.

---

## 8. Phase Roadmap

| Phase | Deliverable | Repository | Implementation |
|-------|-------------|------------|----------------|
| **3A** | Organization Architecture document (this file) | Hermes Agent `docs/` | **Now** |
| **3B** | Worker Manager model + interface | `agent/coo/worker_manager.py` | Models, ABC, dry-run selection |
| **3C** | Worker Registry + Organization Chart data | `agent/coo/workers/registry.py` | Static registry, department map |
| **4** | Worker → Skill execution connection | `agent/coo/adapters/` | PipelineAdapter, skill dispatch |
| **5** | Discord natural language → Worker Assignment | Hermes Agent + gateway config | CEO message → COO → Worker Manager → assignments surfaced to CEO |

### 8.1 Phase 3B Scope (next implementation candidate)

- `WorkerManager` class (select, assign, aggregate status)
- Models: `WorkerAssignment`, `DepartmentAssignment`, `WorkerOrganizationResult`
- Extend `COOOrchestrationResult` or `coo_orchestrate` response with worker layer
- Unit tests (unittest, no Repository 2 mutation)

### 8.2 Phase 3C Scope

- `WORKER_REGISTRY` with all department workers
- `DEPARTMENT_REGISTRY` with phase mapping
- Organization chart as data (not hardcoded in COO)

### 8.3 Phase 4 Scope

- `Worker.execute()` invokes skills through `PipelineAdapter`
- Lifecycle transitions: SELECTED → WORKING → COMPLETED / WAITING / FAILED
- Skill results flow back through Worker Manager to COO

### 8.4 Phase 5 Scope

- End-to-end: Discord CEO message → `coo_orchestrate` → worker assignments → CEO report
- Approval WAITING surfaces in Discord with explicit approve/reject commands
- No auto-approve, no auto-publish

---

## Appendix A: Glossary

| Term | Definition |
|------|------------|
| CEO | Human operator issuing intent and approval decisions |
| COO | Hermes orchestration layer (`agent/coo/`) |
| Worker Manager | Middle management between COO and Workers |
| Department | Functional grouping of workers |
| Worker | AI employee owning a skill portfolio |
| Skill | Atomic Execution Engine entrypoint |
| Execution Engine | Repository 2 (`multi-content-pipeline`) |
| Company Memory | Future long-term organizational knowledge layer |
| WAITING | Non-failure pause awaiting CEO, scheduler, or upstream worker |
| CANCELLED | Terminal state from explicit CEO termination (not failure, not policy block) |

---

## Appendix B: Relationship to Existing Phase 2 COO

Phase 2 implemented:

```text
CEO → COO → Intent → Plan → Policy → Skill Selection
```

Phase 3 inserts Worker Manager + Departments + Workers:

```text
CEO → COO → Intent → Plan → Policy → Worker Manager → Workers → Skills
```

Phase 2 `SkillSelector` evolves into Worker Manager staffing + per-worker
skill planning. Existing safeguards (`autoApply=false`, `reviewRequired=true`,
policy BLOCK propagation) carry forward unchanged.

---

## Appendix C: Explicit Non-Changes (All Phases Until CTO Approval)

- `gateway/` — no modification
- `run_agent.py` — no modification
- `conversation_loop.py` — no modification
- Repository 2 — no modification
- `pipeline.js`, `daily-assignment.js` — no modification

---

## Appendix D: Known Future Decisions

| Decision | Deferred to | Notes |
|----------|-------------|-------|
| **Company Memory ↔ Learning Engine integration** | **Phase 4** | Whether Company Memory integrates with the Learning Engine (and in what direction — read-only consumption of learning proposals vs. bidirectional sync) is **not decided in Phase 3A**. Phase 4 will decide based on Worker → Skill execution experience. Until then, Company Memory remains a documented concept with no schema, storage, or provider. |
| Learning Department inline vs post-run | Phase 4 | Whether Learning workers run inside the create flow or strictly post-run |
| Granular skill catalog split | Phase 5 | Decomposing monolithic `create_content` into per-worker skills |
| **Multi-project / multi-tenant** | **Project Layer phase** | Current COO/Worker Runtime assumes **single-project, single-tenant** operation. Multi-project / multi-tenant support will require `project_id` / `tenant_id` across `COOConfig`, `IntentResult`, `ExecutionPlan`, `PolicyDecision`, and `WorkerAssignment`. This is intentionally deferred until the Project Layer phase. |
