# Recovery Runbook

Operator guide for consume recovery, repair, locks, and correlation-assisted
investigation.

<!-- anchor: when-recovery-required -->
## When recovery is required

`consume recovery` and `gateway dashboard` surface `recovery_required` when bundle
and confirmation consumption states are inconsistent. Common consume states:

| State | Meaning |
| ----- | ------- |
| `unconsumed` | No consumption recorded |
| `prepared` | Prepared but not committed |
| `committed` | Successfully consumed |
| `partial` | Partial consumption |
| `legacy_partial` | Legacy partial marker |
| `recovery_required` | Operator intervention needed |

Assess:

```bash
hermes coo dispatch consume status --ticket-id <ticket-id> --confirmation-id <confirmation-id>
hermes coo dispatch consume recovery --ticket-id <ticket-id> --confirmation-id <confirmation-id>
```

## Repair workflow

### 1. Dry-run eligibility

```bash
hermes coo dispatch consume repair dry-run \
  --ticket-id <ticket-id> \
  --confirmation-id <confirmation-id> \
  --operator-id <operator-id> \
  --operator-name <name> \
  --reason "<reason>"
```

Evaluates repair without mutating persisted state.

### 2. Apply (gated)

```bash
hermes coo dispatch consume repair apply \
  --ticket-id <ticket-id> \
  --confirmation-id <confirmation-id> \
  --operator-id <operator-id> \
  --operator-name <name> \
  --reason "<reason>" \
  --phrase <repair-confirmation-phrase>
```

Requires the fixed repair confirmation phrase documented in CLI help. Apply only
after dry-run passes and operator sign-off.

### 3. Repair lock

Before apply, check lock contention:

```bash
hermes coo dispatch consume repair lock status \
  --ticket-id <ticket-id> \
  --confirmation-id <confirmation-id>
```

Dashboard reports `repair_lock_held=true` when repair is in progress.

### 4. Repair audit

```bash
hermes coo dispatch consume repair audit show --repair-attempt-id <repair-attempt-id>
hermes coo dispatch consume repair audit list --ticket-id <ticket-id>
```

<!-- anchor: correlation-during-recovery -->
## Correlation during recovery

Link recovery context to gateway and pilot artifacts:

```bash
hermes coo dispatch gateway correlation show --ticket-id <ticket-id>
hermes coo dispatch gateway audit show --gateway-request-id <gateway-request-id>
```

Use correlation `recommended_action`:

- `resolve_recovery_required`
- `inspect_consume_state`
- `resolve_correlation_mismatch`

<!-- anchor: manual-recovery -->
## Manual recovery operator flow

1. **Confirm scope** — `consume status` + `consume recovery` for the ticket pair.
2. **Check lock** — `consume repair lock status`; wait if lock held.
3. **Correlate** — `gateway correlation show` for latest gateway request.
4. **Dry-run repair** — `consume repair dry-run`; review safe summary output.
5. **Apply** — only with explicit operator phrase after dry-run success.
6. **Verify** — `consume status`, `gateway dashboard`, `gateway correlation diff`
   if multiple requests exist on the ticket.
7. **Audit trail** — `consume repair audit list` for the repair attempt id.

## Fail-closed

- Repair apply without dry-run eligibility fails closed.
- Concurrent repair lock blocks second apply.
- Correlation mismatch blocks dashboard `HEALTHY` until resolved.
- Never bypass consume gates with direct file edits.

## Read-only alternatives

When mutation is not yet authorized, stay read-only:

- `operator runbook --ticket-id <ticket-id> --confirmation-id <confirmation-id>`
- `gateway dashboard --ticket-id <ticket-id>`
- `gateway correlation diff` for drift between two gateway requests

<!-- anchor: phase-15-consume-records -->
## Phase 15 consume records

**This is a different mechanism from everything above.** The recovery flow
described earlier in this document is about the Phase 8–13 dispatch
**bundle/confirmation** consume transaction. Phase 15's permission / runtime
boundary / runtime invocation reservation / execution authorization
artifacts each have their own, separate, write-once **consume record** —
same English word, unrelated store, unrelated CLI surface
(`production governed-cutover ...`, not `consume ...`).

### What a Phase 15 consume record is

Each of the four Phase 15 prerequisites is issued/reserved/authorized as an
immutable artifact. "Consumed" is recorded as a **separate** append-only
file referencing the original artifact's id — the original is never
mutated. There is no CLI command to inspect these records directly in V1;
governed runtime closure validation (Phase 15J, Python API only) is what
cross-checks them.

### Partial consume

If only some of the four consume records exist for an activation (1-of-4,
2-of-4, or 3-of-4), the chain is in a **partial consume** state. This is
never auto-repaired. Operator action:

1. Do **not** delete or hand-edit any existing consume record or any of the
   four original Phase 15 artifacts.
2. Escalate for manual inspection (`inspect_partial_governed_consume` is the
   internal recommended-action code surfaced by governed runtime closure
   evaluation).
3. Do not attempt to re-run the governed invoke step to "finish" a partial
   chain — Phase 15I/15J have no CLI-exposed retry path in V1 by design.

### Replay detection

If a consume record's recorded governed-invoke correlation id does not match
the activation's single governed invoke record, this is **replay** — a sign
that consumption happened out of the expected one-shot sequence. Treat as a
hard stop: no closure artifact should be produced, and the situation requires
manual investigation, not automatic resolution
(`resolve_governed_runtime_replay`).

### Append-only artifact deletion is always prohibited

This applies to every artifact in both the Phase 8–13 dispatch chain and the
Phase 14/15 governance chain: bundles, confirmations, activation proposals,
cutover contracts, window events, permission/session/boundary/invocation/
authorization/runtime-start records, consume records, and closure records.
Never delete or overwrite any of them to "clear" a bad state — the audit
trail is the point.
