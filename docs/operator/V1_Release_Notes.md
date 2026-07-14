# V1 Release Notes

## Release candidate

- Tag candidate: `v1.0.0-rc.1` (not yet tagged or pushed)
- Validated HEAD: `68a310e5c`
- Status: `V1_RC_READY_WITH_WARNINGS` — see
  [V1_Release_Candidate_Validation.md](V1_Release_Candidate_Validation.md)

## Summary

Hermes COO dispatch V1 adds a complete, append-only **production activation
governance chain** (Phase 14A–15J) on top of the existing dispatch/gateway/
pilot/recovery operator surface. Every stage of the new chain is read-only
evaluation plus an append-only artifact write; nothing in it enables real
production execution. `production_execution_allowed` and
`production_root_hard_deny` remain at their safe defaults (`false` /
`true`) throughout.

## What's new

- **Phase 14 Production Activation Governance** (`production
  activation ...`): proposal, multi-party approval, arm with TTL, active
  gate, dry-run, controlled active transition, execution gate, an isolated
  mirror live pilot (the only step in the entire system that runs a real,
  tightly bounded subprocess — and only against a `/tmp` mirror, never the
  real Repository2 checkout), live E2E finalize, operational sign-off,
  rollback validation, and final sign-off.
- **Phase 15 Governed Production Chain** (`production
  governed-cutover ...`): governed cutover contract, controlled window,
  runtime permission, governed runtime session, runtime boundary, runtime
  invocation reservation, execution authorization, and runtime start —
  each a distinct one-shot gate with its own append-only artifact.
- **Governed runtime invoke bookkeeping** (Phase 15I) and **governed
  runtime closure validation** (Phase 15J): complete and correct as Python
  APIs; intentionally not exposed via CLI in V1.
- Full operator documentation for the above — see
  [Architecture_Overview.md](Architecture_Overview.md) and
  [CLI_Command_Reference.md](CLI_Command_Reference.md).

## What's explicitly not included

See [V1_Scope_Freeze.md](V1_Scope_Freeze.md) for the complete list.
Highlights: no real Repository2 execution, no live Gateway/Discord
production dispatch, no external publish, no automatic window/session
closure, no invocation-completion lifecycle, no CLI for Phase 15I/15J.

## Known warnings carried into V1

See
[V1_Release_Candidate_Validation.md](V1_Release_Candidate_Validation.md#known-warnings)
for the full table and classification (V1.1 backlog vs. operational
mitigation). None are release blockers.

## Upgrade / rollback notes

No schema or artifact format changes are required to adopt this release.
Rollback guidance (including the principle of never deleting append-only
artifacts on rollback) is in
[V1_Release_Candidate_Validation.md](V1_Release_Candidate_Validation.md#rollback-guidance).
