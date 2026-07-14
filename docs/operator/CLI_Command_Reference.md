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
| `production` | Legacy readiness/sign-off/cutover-check, plus Phase 14 `activation` and Phase 15 `governed-cutover` governance chains |
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
hermes coo dispatch operator guidance --recommended-action <code>
```

`operator guidance` is read-only: it maps a `recommended_action` code to
`runbook_ref` and `guidance_summary` without emitting shell commands.

## `repository`

```
hermes coo dispatch repository attest --repository-root <absolute-path>
```

## `production`

`production` has two generations of commands. **Legacy** (`readiness` /
`sign-off` / `cutover-check` / `final-signoff*`, Phase 13D) evaluates isolated
pilot-fleet readiness. **Governed** (`activation`, `governed-cutover`, Phase
14A–15H) is the append-only production activation governance chain. Both
namespaces coexist without conflict; neither sets
`production_execution_allowed=true`.

### Legacy (Phase 13D)

```
hermes coo dispatch production readiness
hermes coo dispatch production sign-off
hermes coo dispatch production cutover-check [--ticket-id <ticket-id>] [--limit <N>]
hermes coo dispatch production final-signoff-status
hermes coo dispatch production final-signoff
```

### `production activation` (Phase 14 — Production Activation Governance)

Read-only unless noted. Append-only artifacts throughout; no subprocess, no
Repository2 access at any step.

```
hermes coo dispatch production activation propose ...          # append-only proposal artifact
hermes coo dispatch production activation approve ...          # one release-approver approval
hermes coo dispatch production activation security-review ...  # security reviewer approval
hermes coo dispatch production activation status ...
hermes coo dispatch production activation arm ...               # arm with executor confirmation + TTL
hermes coo dispatch production activation disarm ...             # disarm/cancel armed activation
hermes coo dispatch production activation gate ...                # active-gate readiness (read-only)
hermes coo dispatch production activation suspend ...              # kill switch: suspend armed activation
hermes coo dispatch production activation revoke ...                 # kill switch: revoke suspended activation
hermes coo dispatch production activation dry-run ...                 # dry-run contract (read-only)
hermes coo dispatch production activation activate ...                 # armed -> active transition (no execution)
hermes coo dispatch production activation active-status ...
hermes coo dispatch production activation execution-gate ...            # pre-execution gate (read-only)
hermes coo dispatch production activation live-pilot ...                 # isolated mirror live pilot preflight/reserve
hermes coo dispatch production activation live-pilot-finalize ...
hermes coo dispatch production activation live-pilot-status ...
hermes coo dispatch production activation live-pilot-signoff ...          # operational sign-off record
hermes coo dispatch production activation rollback-check ...
hermes coo dispatch production activation rollback-plan ...
```

`live-pilot` is the only path in the entire CLI that can run a **bounded
subprocess** — and only against an isolated `/tmp` mirror of the pipeline
root, never the real Repository2 checkout, and only inside an ephemeral
execution permit. See
[Architecture_Overview.md](Architecture_Overview.md#phase-14-production-activation-governance)
for the full state sequence and the `isolated_mirror_runtime_invoked` /
`original_repository2_execution_attempted` distinction.

### `production governed-cutover` (Phase 15A–15H — Governed Production Chain)

Distinct from legacy `production cutover-check`. Every subcommand below is
read-only evaluation (`status`/`check`/`show`/`history`) except the single
state-advancing verb per stage (`prepare`/`open`/`close`/`emergency-close`/
`issue`/`start`/`reserve`/`authorize`). No step in this chain sets
`production_execution_allowed=true` or touches Repository2.

```
hermes coo dispatch production governed-cutover status
hermes coo dispatch production governed-cutover check
hermes coo dispatch production governed-cutover prepare ...   # append-only cutover contract
hermes coo dispatch production governed-cutover show

hermes coo dispatch production governed-cutover window status
hermes coo dispatch production governed-cutover window history
hermes coo dispatch production governed-cutover window open ...
hermes coo dispatch production governed-cutover window close ...
hermes coo dispatch production governed-cutover window emergency-close ...

hermes coo dispatch production governed-cutover permission status
hermes coo dispatch production governed-cutover permission check
hermes coo dispatch production governed-cutover permission issue ...
hermes coo dispatch production governed-cutover permission show
hermes coo dispatch production governed-cutover permission history

hermes coo dispatch production governed-cutover session status
hermes coo dispatch production governed-cutover session check
hermes coo dispatch production governed-cutover session start ...
hermes coo dispatch production governed-cutover session show
hermes coo dispatch production governed-cutover session history

hermes coo dispatch production governed-cutover runtime-boundary status
hermes coo dispatch production governed-cutover runtime-boundary check
hermes coo dispatch production governed-cutover runtime-boundary prepare ...
hermes coo dispatch production governed-cutover runtime-boundary show
hermes coo dispatch production governed-cutover runtime-boundary history

hermes coo dispatch production governed-cutover runtime-invocation status
hermes coo dispatch production governed-cutover runtime-invocation check
hermes coo dispatch production governed-cutover runtime-invocation reserve ...
hermes coo dispatch production governed-cutover runtime-invocation show
hermes coo dispatch production governed-cutover runtime-invocation history

hermes coo dispatch production governed-cutover execution-authorization status
hermes coo dispatch production governed-cutover execution-authorization check
hermes coo dispatch production governed-cutover execution-authorization authorize ...
hermes coo dispatch production governed-cutover execution-authorization show
hermes coo dispatch production governed-cutover execution-authorization history

hermes coo dispatch production governed-cutover runtime-start status
hermes coo dispatch production governed-cutover runtime-start check
hermes coo dispatch production governed-cutover runtime-start start ...
hermes coo dispatch production governed-cutover runtime-start show
hermes coo dispatch production governed-cutover runtime-start history
```

`runtime-start start` sets `runtime_started=true` on its own new contract
record only — it does **not** invoke any runtime and does **not** consume the
underlying permission or authorization. See
[Architecture_Overview.md](Architecture_Overview.md#phase-15-governed-production-chain)
for what each stage actually asserts.

### Internal-only (no CLI — Python API only)

Phase 15I (`agent/coo/production_governed_runtime_invoke.py`) and Phase 15J
(`agent/coo/production_governed_runtime_closure.py`) are **not** exposed
anywhere in this CLI tree. There is no `invoke` or `closure` subcommand under
`production governed-cutover` or elsewhere. Both remain Python-API-only for
V1. See [V1_Scope_Freeze.md](V1_Scope_Freeze.md).

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

<!-- anchor: gateway -->
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
- [Architecture_Overview.md](Architecture_Overview.md) — Phase 14/15 governance flow
- [V1_Scope_Freeze.md](V1_Scope_Freeze.md) — what is and is not in V1
- [V1_Release_Candidate_Validation.md](V1_Release_Candidate_Validation.md)
