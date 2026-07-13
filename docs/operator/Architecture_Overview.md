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
