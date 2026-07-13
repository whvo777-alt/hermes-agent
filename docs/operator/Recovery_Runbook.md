# Recovery Runbook

Operator guide for consume recovery, repair, locks, and correlation-assisted
investigation.

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
