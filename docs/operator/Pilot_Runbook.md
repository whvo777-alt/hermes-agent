# Pilot Runbook

Operator guide for isolated operational pilot, history, regression, and fleet
review.

## Pilot modes

| Mode | Command pattern | Runner invoked |
| ---- | ---------------- | -------------- |
| Dry run | `pilot run --dry-run` | No |
| Live mock | `pilot run` (no dry-run) | Injected mock runner only |
| Gateway pilot dry run | `gateway pilot run --dry-run` | No |

Production execution remains **disabled**. All pilot paths use isolated roots
rejected for production repository attestation.

## Readiness

```bash
hermes coo dispatch pilot readiness
hermes coo dispatch pilot readiness --ticket-id <ticket-id> --confirmation-id <confirmation-id>
```

Gateway-scoped readiness:

```bash
hermes coo dispatch gateway pilot readiness --session-id <session-id> ...
```

<!-- anchor: history -->
## History

```bash
hermes coo dispatch pilot history list
hermes coo dispatch pilot history show --pilot-attempt-id <pilot-attempt-id>
hermes coo dispatch pilot history find --ticket-id <ticket-id>
```

History records include status, evidence flags, audit flags, consume flag, and
opaque correlation ids.

<!-- anchor: regression -->
## Regression

Fleet regression summary:

```bash
hermes coo dispatch pilot regression
hermes coo dispatch pilot regression --ticket-id <ticket-id>
```

Statuses: `PASS`, `WARN`, `FAIL`. Dashboard treats consecutive failures and
`FAIL` as blocking signals.

## Trend

```bash
hermes coo dispatch pilot runbook
hermes coo dispatch pilot runbook --ticket-id <ticket-id>
```

Trend statuses include `STABLE`, `DEGRADED`, `INSUFFICIENT_DATA`.

## Dashboard integration

```bash
hermes coo dispatch gateway dashboard
```

Pilot regression and trend feed `dashboard_health` and `recommended_action`.

## Fleet review

```bash
hermes coo dispatch pilot fleet --ticket-id <ticket-id>
hermes coo dispatch pilot fleet --ticket-id <ticket-id> --limit <N>
```

Repeat `--ticket-id` for multiple tickets in cutover review.

## Cutover checklist

```bash
hermes coo dispatch production cutover-check
hermes coo dispatch production cutover-check --ticket-id <ticket-id>
```

Cutover ready does **not** enable production execution. It confirms isolated
pilot fleet readiness only.

## Operator sequence (new staged gateway)

1. `gateway status` — confirm `staged`.
2. `gateway readiness` — resolve blocked checks.
3. `pilot regression` — ensure not `FAIL`.
4. `gateway pilot run --dry-run` — preflight correlation.
5. `gateway pilot run` — live mock when regression allows.
6. `gateway audit show` — verify chain.
7. `gateway dashboard` — confirm `HEALTHY` or acceptable `DEGRADED`.

## Exit codes

- Regression CLI may exit `1` on `FAIL`.
- Dashboard exits `1` on `BLOCKED`.
- Correlation exits `1` on mismatch, ambiguity, or regression drift (diff).
