# Gateway Runbook

Operator guide for gateway enablement, mock pilot, and read-only observability.

## Gateway state

| State | Meaning | Operator note |
| ----- | ------- | ------------- |
| `disabled` | Gateway UI and execution surfaces off | Stage before mock pilot |
| `staged` | Mock-only gateway pilot supported | **Normal operator target** |
| `enabled` | Not supported for production dispatch | Treated as **blocked** in dashboard |

Check current state:

```bash
hermes coo dispatch gateway status
```

## Readiness

Evaluates whether gateway mock dispatch prerequisites pass:

```bash
hermes coo dispatch gateway readiness
hermes coo dispatch gateway readiness --ticket-id <ticket-id> --confirmation-id <confirmation-id>
```

Optional evidence cross-reference requires ticket, confirmation, and an isolated
pipeline root argument (production roots rejected).

## Status and facade

```bash
hermes coo dispatch gateway status
hermes coo dispatch gateway facade
```

Status covers enablement flags. Facade reports connection scaffold for isolated
mock execution (`facade_connected`, `isolated_execution_supported`).

## Gateway pilot

Staged mock-only pilot path:

```bash
hermes coo dispatch gateway pilot readiness \
  --session-id <session-id> \
  --ticket-id <ticket-id> \
  --confirmation-id <confirmation-id> \
  ...

hermes coo dispatch gateway pilot run --dry-run ...
```

`--dry-run` performs preflight only; runner not invoked, nothing consumed.

## Dashboard

Aggregated operator health:

```bash
hermes coo dispatch gateway dashboard
hermes coo dispatch gateway dashboard --ticket-id <ticket-id> --session-id <session-id>
```

Health values: `HEALTHY`, `DEGRADED`, `BLOCKED`, `NOT_CONFIGURED`.

Exit code `1` when `BLOCKED`.

## Correlation explorer

Resolve full chain from any single opaque id:

```bash
hermes coo dispatch gateway correlation show --gateway-request-id <id>
hermes coo dispatch gateway correlation show --pilot-attempt-id <id>
hermes coo dispatch gateway correlation show --execution-attempt-id <id>
hermes coo dispatch gateway correlation show --dispatch-run-id <id>
hermes coo dispatch gateway correlation show --ticket-id <id>
```

Exactly **one** query id per invocation.

Compare two requests on the same ticket:

```bash
hermes coo dispatch gateway correlation diff \
  --left-gateway-request-id <older-id> \
  --right-gateway-request-id <newer-id>
```

## Request audit

Per-request correlation summary:

```bash
hermes coo dispatch gateway audit show --gateway-request-id <gateway-request-id>
```

## Operational status (Discord)

Discord surfaces mirror CLI summaries: health, regression, trend, timeline, and
recommended action codes. Use messaging status when CLI access is inconvenient;
semantics match read-only CLI builders.

## Recommended action codes (gateway)

| Code | Meaning |
| ---- | ------- |
| `no_action_required` | Chain complete, policy nominal |
| `run_gateway_pilot_dry_run` | Dry-run history only; collect live mock preflight |
| `collect_more_history` | Insufficient pilot history |
| `inspect_latest_failure` | Recent failure needs review |
| `inspect_missing_evidence` | Evidence or audit gap |
| `resolve_recovery_required` | Consume recovery open |
| `resolve_correlation_mismatch` | ID mismatch across stores |
| `stage_gateway` | Gateway disabled |
| `maintain_production_block` | Policy violation detected |

## Policy

- `production_execution_allowed=false`
- `production_root_hard_deny=true`
- `gateway_execution_scope=isolated_gateway_mock`
