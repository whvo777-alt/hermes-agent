# CLI Command Reference

Complete `hermes coo dispatch` command tree as implemented. All commands run under:

```bash
hermes coo dispatch <subcommand> ...
```

**Production warning:** `production_execution_allowed` remains false. Production
repository roots are hard-denied. Prefer read-only commands for investigation.

## Top-level commands

| Command | Purpose |
| ------- | ------- |
| `confirm-run` | Create confirmation record (no dispatch execution) |
| `run` | Gated dispatch from bundle + confirmation |
| `status` | Read-only bundle / confirmation summary |
| `readiness` | Operator readiness preflight |
| `config validate` | Validate executor config |
| `audit` | Dispatch execution audit records |
| `evidence` | Execution attempt evidence |
| `consume` | Consume transaction status, recovery, repair |
| `operator runbook` | Read-only operator runbook for consume pair |
| `repository attest` | Read-only repository attestation |
| `production` | Production readiness, sign-off, cutover |
| `pilot` | Isolated operational pilot |
| `enablement check` | Runner enablement assessment |
| `gateway` | Gateway status, pilot, audit, correlation, dashboard |
| `binding` | Runner binding state (status / stage / reset / bind) |

---

## `confirm-run`

Create production executor confirmation. Does not run dispatch.

```
hermes coo dispatch confirm-run \
  --ticket-id <ticket-id> \
  --plan-id <plan-id> \
  --unlock-token-id <unlock-token-id> \
  --dispatch-request-id <dispatch-request-id> \
  --operator-id <operator-id> \
  --operator-name <name> \
  --reason "<reason>" \
  --phrase <confirmation-phrase> \
  --pipeline-root <isolated-root>
```

## `run`

```
hermes coo dispatch run \
  --ticket-id <ticket-id> \
  --unlock-token-id <unlock-token-id> \
  --confirmation-id <confirmation-id> \
  --requester-id <requester-id> \
  --pipeline-root <isolated-root> \
  [--dry-run]
```

## `status` / `readiness`

```
hermes coo dispatch status --ticket-id <ticket-id> [--confirmation-id <id>] [--pipeline-root <root>]
hermes coo dispatch readiness --ticket-id <ticket-id> --confirmation-id <id> --pipeline-root <root>
```

## `config`

```
hermes coo dispatch config validate
```

## `audit`

```
hermes coo dispatch audit show --dispatch-run-id <dispatch-run-id>
hermes coo dispatch audit list
hermes coo dispatch audit find --ticket-id <ticket-id>
```

## `evidence`

```
hermes coo dispatch evidence show --execution-attempt-id <execution-attempt-id>
hermes coo dispatch evidence find --ticket-id <ticket-id>
```

## `consume`

```
hermes coo dispatch consume status --ticket-id <ticket-id> --confirmation-id <confirmation-id>
hermes coo dispatch consume recovery --ticket-id <ticket-id> --confirmation-id <confirmation-id>

hermes coo dispatch consume repair dry-run --ticket-id <ticket-id> --confirmation-id <confirmation-id> ...
hermes coo dispatch consume repair apply --ticket-id <ticket-id> --confirmation-id <confirmation-id> ...

hermes coo dispatch consume repair audit show --repair-attempt-id <repair-attempt-id>
hermes coo dispatch consume repair audit list [--ticket-id <ticket-id>]

hermes coo dispatch consume repair lock status --ticket-id <ticket-id> --confirmation-id <confirmation-id>
```

## `operator`

```
hermes coo dispatch operator runbook --ticket-id <ticket-id> --confirmation-id <confirmation-id>
```

## `repository`

```
hermes coo dispatch repository attest --repository-root <absolute-path>
```

## `production`

```
hermes coo dispatch production readiness
hermes coo dispatch production sign-off
hermes coo dispatch production cutover-check [--ticket-id <ticket-id>] [--limit <N>]
```

## `pilot`

```
hermes coo dispatch pilot readiness [--pipeline-root <root>] [--ticket-id <id>] [--confirmation-id <id>]
hermes coo dispatch pilot run ... [--dry-run]
hermes coo dispatch pilot regression [--ticket-id <ticket-id>] [--limit <N>]
hermes coo dispatch pilot runbook [--ticket-id <ticket-id>]
hermes coo dispatch pilot fleet [--ticket-id <ticket-id>] [--limit <N>]

hermes coo dispatch pilot history show --pilot-attempt-id <pilot-attempt-id>
hermes coo dispatch pilot history list
hermes coo dispatch pilot history find --ticket-id <ticket-id>
```

## `enablement`

```
hermes coo dispatch enablement check
```

## `gateway`

```
hermes coo dispatch gateway status
hermes coo dispatch gateway readiness [--ticket-id <id>] [--confirmation-id <id>] [--pipeline-root <root>]
hermes coo dispatch gateway facade

hermes coo dispatch gateway pilot readiness --session-id <id> --ticket-id <id> --confirmation-id <id> ...
hermes coo dispatch gateway pilot run ... [--dry-run]

hermes coo dispatch gateway audit show --gateway-request-id <gateway-request-id>

hermes coo dispatch gateway correlation show \
  [--gateway-request-id <id> | --pilot-attempt-id <id> | --execution-attempt-id <id> | \
   --dispatch-run-id <id> | --ticket-id <id>]

hermes coo dispatch gateway correlation diff \
  --left-gateway-request-id <id> \
  --right-gateway-request-id <id>

hermes coo dispatch gateway dashboard [--ticket-id <id>] [--session-id <id>] [--limit <N>]
```

## `binding`

```
hermes coo dispatch binding status
hermes coo dispatch binding stage --operator-id <id> --reason "<reason>"
hermes coo dispatch binding reset --operator-id <id> --reason "<reason>"
hermes coo dispatch binding bind --operator-id <id> --reason "<reason>"
```

---

## Help

```bash
hermes coo dispatch --help
hermes coo dispatch gateway --help
hermes coo dispatch gateway dashboard --help
```

## Safe usage

- Use opaque ids from CLI output or approval summaries only.
- Prefer `--dry-run` for first execution on a new ticket.
- Treat exit code `1` on dashboard, correlation, and regression as operator signals.
- Never paste secrets, phrases, or repository paths into tickets or logs.

## See also

- [Dispatch_Runbook.md](Dispatch_Runbook.md)
- [Gateway_Runbook.md](Gateway_Runbook.md)
- [Recovery_Runbook.md](Recovery_Runbook.md)
- [Pilot_Runbook.md](Pilot_Runbook.md)
