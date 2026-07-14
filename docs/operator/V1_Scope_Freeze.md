# V1 Scope Freeze

Frozen at HEAD `68a310e5c`. This document is the authoritative boundary of
what Hermes COO dispatch V1 does and does not do. If a behavior is not
listed as included below, treat it as excluded regardless of what code
happens to exist.

## V1 included scope

- COO orchestration (`agent/coo/`)
- Dispatch planning/policy (bundle, confirmation, executor policy)
- Approval/session (Discord approval binding, gateway session)
- Consume/recovery/repair (Phase 8–13 dispatch bundle/confirmation consume
  transaction — see [Recovery_Runbook.md](Recovery_Runbook.md))
- Audit/evidence/correlation (dispatch run audit, execution evidence,
  cross-id correlation lookup)
- Gateway mock/staged operational surface (`gateway status`, `gateway
  pilot`, never `enabled` for live production)
- Discord approval/status UX
- Pilot/fleet/cutover readiness (isolated mock dispatch attempts)
- Operator dashboard (`gateway dashboard`)
- Operator runbooks (`operator runbook`, `operator guidance`)
- Production activation governance — Phase 14A–14J (`production
  activation ...`): proposal, multi-party approval, arm/TTL, active gate,
  dry-run, controlled active transition, execution gate, isolated mirror
  live pilot, live E2E finalize, operational sign-off, rollback validation,
  final sign-off
- Governed cutover/window/permission/session/boundary/invocation — Phase
  15A–15F (`production governed-cutover ...`)
- Execution authorization/runtime start — Phase 15G–15H (`production
  governed-cutover execution-authorization ...` /
  `... runtime-start ...`)
- Governed runtime invoke bookkeeping — Phase 15I
  (`agent/coo/production_governed_runtime_invoke.py`, Python API only, no
  CLI)
- Governed runtime closure validation — Phase 15J
  (`agent/coo/production_governed_runtime_closure.py`, Python API only, no
  CLI)

## V1 excluded scope

- Repository2 original production execution (the real
  `/opt/data/multi-content-pipeline` checkout is never run or modified by
  anything in V1, including `live-pilot`, which only ever targets an
  isolated `/tmp` mirror)
- `production_execution_allowed=true` (no code path in V1 sets this to
  true outside of test-only `force_*` override parameters, which are never
  reachable from any CLI command)
- Live Gateway production execution (`gateway enabled=true` is
  unsupported)
- Live Discord production execution
- External publish (WordPress/API publish of any kind)
- Automatic scheduler production dispatch
- Multi-ticket production batch dispatch
- Permanent production enablement of any kind
- Auto recovery / auto rollback (all recovery and rollback actions in V1
  require explicit operator invocation; see
  [Recovery_Runbook.md](Recovery_Runbook.md))
- Automatic window close / automatic session close (operators must close
  a controlled window manually; there is no automatic session-close
  trigger in V1)
- Actual session-close / invocation-complete lifecycle transitions (Phase
  15D session and Phase 15F invocation reservation readiness are
  evaluated read-only in V1; the actual state transitions that would mark
  them closed/completed are not implemented — planned for a later
  release)
- CLI exposure of Phase 15I (`governed_runtime_invoke`) and Phase 15J
  (`governed_runtime_closure`) internal Python APIs — both remain
  Python-API-only

## Runtime terminology

These four fields must never be conflated into a single ambiguous
`runtime_invoked` key in any V1 output, log, or dashboard:

| Field | What it actually means |
| ----- | ----------------------- |
| `runtime_started` | A Phase 15H contract record exists. Not a process execution signal. |
| `governed_runtime_invoked` | Phase 15I bookkeeping completed (all four Phase 15 consume steps succeeded). Not a process execution signal. |
| `isolated_mirror_runtime_invoked` (a.k.a. `phase14_runtime_invoked`) | A Phase 14 `live-pilot` bounded subprocess actually ran, against an isolated `/tmp` mirror only. This is the only field of the four that ever reflects a real OS-level process. |
| `original_repository2_execution_attempted` | Always `false` in V1. There is no code path that flips this. |

See
[Architecture_Overview.md](Architecture_Overview.md#phase-15-governed-production-chain)
for where each field is produced.

## Related documents

- [Architecture_Overview.md](Architecture_Overview.md)
- [CLI_Command_Reference.md](CLI_Command_Reference.md)
- [V1_Release_Candidate_Validation.md](V1_Release_Candidate_Validation.md)
- [V1_Release_Notes.md](V1_Release_Notes.md)
