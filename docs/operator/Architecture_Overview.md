# Architecture Overview

High-level read-only view of the COO dispatch operator stack. No execution is
implied by this diagram; it documents how persisted artifacts relate.

## Flow

```
CEO
 ↓
Discord (gateway messaging surface)
 ↓
Approval (session + ticket binding)
 ↓
Dispatch (bundle + confirmation preparation)
 ↓
Gateway (staged enablement + request records)
 ↓
Pilot (isolated mock dispatch attempts)
 ↓
Execution (gated; production blocked by policy)
 ↓
Evidence (execution attempt artifacts)
 ↓
Audit (dispatch run records)
 ↓
Consume (bundle + confirmation transaction)
 ↓
Repair (consume drift remediation)
 ↓
History (pilot attempt persistence)
 ↓
Correlation (cross-ID chain lookup)
 ↓
Dashboard (operator health summary)
```

## Planes

| Plane | Role | Primary CLI |
| ----- | ---- | ----------- |
| Approval | Binds CEO session to execution ticket | Discord status actions |
| Dispatch | Prepares bundle and confirmation | `dispatch status`, `dispatch run` |
| Gateway | Stages mock-only execution requests | `gateway status`, `gateway pilot` |
| Pilot | Exercises isolated operational path | `pilot run`, `pilot history` |
| Recovery | Resolves consume partial states | `consume recovery`, `consume repair` |
| Observability | Read-only audit and correlation | `gateway audit`, `gateway correlation` |

## Persistence boundaries

- **Ticket** — dispatch bundle key; scopes consume and repair.
- **Gateway request** — idempotent gateway execution record.
- **Pilot attempt** — history row linking gateway request to execution attempt.
- **Execution attempt** — evidence and audit key.
- **Dispatch run** — audit correlation identifier.

## Policy invariants

- `production_execution_allowed=false` at all operator surfaces.
- `production_root_hard_deny=true` — production repository roots rejected.
- Gateway `enabled` is intentionally unsupported for operator mock pilot.
- Correlation and dashboard commands are **read-only**; they never mutate stores.

---

## Phase 14 Production Activation Governance

A separate, append-only governance layer sitting above the dispatch plane
above. Every stage writes a new artifact; none mutate an earlier one; none
set `production_execution_allowed=true`.

```
Proposal (propose)
 ↓
Multi-party approval (approve + security-review, quorum)
 ↓
Arm (executor confirmation + TTL)
 ↓
Active gate (read-only pre-active check)
 ↓
Dry-run (read-only, no execution)
 ↓
Controlled active transition (activate — armed → active, no execution)
 ↓
Execution gate (read-only pre-execution check)
 ↓
Isolated mirror live pilot (live-pilot — the ONLY bounded-subprocess step,
                             and only against a /tmp mirror, never Repository2)
 ↓
Live E2E finalize (evidence + audit + correlation + consume-transaction link)
 ↓
Operational sign-off (live-pilot-signoff — append-only human sign-off)
 ↓
Rollback validation (rollback-check / rollback-plan — read-only)
 ↓
Final sign-off (synthesizes the full chain; never grants execution)
```

Kill switch (`suspend` / `revoke`) can interrupt an armed/active activation at
any point and is itself append-only (control audit events, no destructive
mutation).

## Phase 15 Governed Production Chain

Built on top of a completed Phase 14 final sign-off. CLI: `production
governed-cutover ...` (see
[CLI_Command_Reference.md](CLI_Command_Reference.md#production-governed-cutover-phase-15a15h--governed-production-chain)).
Each stage is a distinct, sequential, one-shot gate — a later stage existing
never implies an earlier one was "used up" in the sense of real execution.

```
Governed cutover contract (prepare)      — read-only readiness synthesis + append-only contract
 ↓
Controlled window (open / close / emergency-close) — append-only lifecycle events
 ↓
Runtime permission (issue)               — one-shot append-only prerequisite, bound to an open window
 ↓
Governed runtime session (start)         — one-shot, bound to an unconsumed permission
 ↓
Runtime boundary (prepare)               — one-shot, bound to a started session
 ↓
Runtime invocation reservation (reserve) — one-shot RESERVATION only, not invocation itself
 ↓
Execution authorization (authorize)      — one-shot authorization, not invocation itself
 ↓
Runtime start (start)                    — one-shot contract; `runtime_started=true` on this
 ↓                                          record only, still not invocation
Governed runtime invoke (bookkeeping, Python API only, Phase 15I)
 ↓
Governed runtime closure (validation, Python API only, Phase 15J)
```

Every one of the four consumable prerequisites (permission / runtime
boundary / runtime invocation reservation / execution authorization) is
"consumed" only as a **separate, write-once artifact** — the original
issued/reserved/authorized record is never mutated. See
[Recovery_Runbook.md](Recovery_Runbook.md#phase-15-consume-records) for how
this differs from the Phase 14 dispatch bundle/confirmation "consume"
concept, which is a different, older mechanism that happens to share the
same English word.

### Naming invariants (never merge these into one field)

| Field | Meaning | Set by |
| ----- | ------- | ------ |
| `runtime_started` | The Phase 15H contract record exists. **Not** a subprocess execution signal. | `production governed-cutover runtime-start start` |
| `governed_runtime_invoked` | Phase 15I bookkeeping: the four consume steps completed. **Not** a subprocess execution signal. | Phase 15I Python API only (no CLI) |
| `isolated_mirror_runtime_invoked` / `phase14_runtime_invoked` | Phase 14's `live-pilot` step actually ran a bounded subprocess against an isolated `/tmp` mirror. This is the only one of these fields that ever reflects real process execution. | `production activation live-pilot` |
| `original_repository2_execution_attempted` | Always `false` in every artifact produced by this codebase in V1. | n/a — invariant, never flips |
| `production_execution_allowed` | Always `false` in every artifact and every CLI summary in V1. | n/a — invariant, never flips |

A dashboard, log, or report that shows a bare, unqualified `runtime_invoked`
key (rather than one of the four qualified names above) is a documentation
or tooling bug — see
[V1_Scope_Freeze.md](V1_Scope_Freeze.md#runtime-terminology).
