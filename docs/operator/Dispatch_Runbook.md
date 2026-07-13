# Dispatch Runbook

Operator guide for the core dispatch lifecycle. Commands are under
`hermes coo dispatch`. Unless noted, commands are read-only or gated.

## Lifecycle

### 1. Dispatch ticket

An execution **ticket** identifies the dispatch bundle on disk. Obtain the opaque
ticket id from the approval flow or `dispatch status`.

### 2. Approval

CEO approval binds a messaging session to the ticket. Discord operational status
surfaces session readiness without exposing secrets.

### 3. Confirmation

`dispatch confirm-run` creates a **confirmation record** for a future gated run.
It does **not** execute dispatch. Requires operator identity and an attested
isolated pipeline root (production roots are rejected).

### 4. Dispatch bundle

The bundle captures plan metadata for the ticket. Inspect with:

```bash
hermes coo dispatch status --ticket-id <ticket-id>
```

### 5. Pilot (isolated)

Before gateway pilot, use operational pilot for isolated mock exercises:

```bash
hermes coo dispatch pilot readiness --ticket-id <ticket-id> --confirmation-id <confirmation-id>
hermes coo dispatch pilot run --dry-run ...
```

### 6. Execution (gated)

`dispatch run` loads bundle + confirmation and invokes the injected runner only
when all gates pass. Use `--dry-run` for validation without consumption.

**Production execution remains disabled by policy.**

### 7. Evidence

After an execution attempt:

```bash
hermes coo dispatch evidence show --execution-attempt-id <execution-attempt-id>
hermes coo dispatch evidence find --ticket-id <ticket-id>
```

### 8. Consume

Tracks bundle + confirmation consumption state:

```bash
hermes coo dispatch consume status --ticket-id <ticket-id> --confirmation-id <confirmation-id>
```

States include `unconsumed`, `prepared`, `committed`, `partial`, `recovery_required`.

### 9. Recovery

When consume is inconsistent:

```bash
hermes coo dispatch consume recovery --ticket-id <ticket-id> --confirmation-id <confirmation-id>
```

See [Recovery_Runbook.md](Recovery_Runbook.md).

### 10. Audit

Dispatch execution audits:

```bash
hermes coo dispatch audit show --dispatch-run-id <dispatch-run-id>
hermes coo dispatch audit list
hermes coo dispatch audit find --ticket-id <ticket-id>
```

### 11. Correlation

Gateway correlation links request → pilot → execution → consume:

```bash
hermes coo dispatch gateway correlation show --gateway-request-id <gateway-request-id>
```

### 12. Dashboard

Operator health at a glance:

```bash
hermes coo dispatch gateway dashboard
hermes coo dispatch gateway dashboard --ticket-id <ticket-id>
```

## Operator actions

| Situation | Action |
| --------- | ------ |
| Pre-flight check | `dispatch readiness`, `dispatch status` |
| Staged mock only | `pilot run --dry-run`, `gateway pilot run --dry-run` |
| Post-run verification | `evidence show`, `audit show` |
| Consume drift | `consume recovery`, then [Recovery_Runbook.md](Recovery_Runbook.md) |
| Cross-ID investigation | `gateway correlation show` |
| Fleet health | `gateway dashboard`, `pilot regression` |

## Read-only review commands

- `dispatch readiness`
- `dispatch status`
- `dispatch audit` / `evidence`
- `dispatch consume status` / `recovery`
- `operator runbook`
- `production readiness`, `production sign-off`

## Fail-closed reminders

- Malformed or path-like IDs are rejected.
- Production root attestation always fails closed.
- Missing bundle or confirmation records return explicit errors, not partial success.
