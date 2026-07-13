# Production Sign-off

Why production dispatch remains blocked and how to interpret sign-off gates.

## Current production posture

Production execution is **intentionally disabled**:

| Policy flag | Value | Meaning |
| ----------- | ----- | ------- |
| `production_execution_allowed` | `false` | No live production runner dispatch |
| `production_root_hard_deny` | `true` | Production repository roots rejected |
| Gateway `enabled` | unsupported | Mock pilot requires `staged` only |

Sign-off and cutover readiness describe **isolated operator readiness**, not
permission to run production pipelines.

## Production readiness

```bash
hermes coo dispatch production readiness
```

Evaluates capability checks: executor config, binding, gateway enablement,
facade, attestation blocks, and intentional production denials.

## Repository attestation

Read-only structural attestation (stat/read/hash only; no execution):

```bash
hermes coo dispatch repository attest --repository-root <absolute-path>
```

Production repository roots fail attestation for execution purposes even when
structure checks pass. Attestation supports identity verification, not dispatch.

## Gateway readiness

```bash
hermes coo dispatch gateway readiness
```

Includes `production_signoff_ready` and `production_cutover_ready` cross-checks
when evidence context is complete.

## Pilot fleet

```bash
hermes coo dispatch pilot fleet --ticket-id <ticket-id>
```

Reviews per-ticket pilot history health for fleet sign-off.

## Sign-off

```bash
hermes coo dispatch production sign-off
```

When `signoff_ready=true`, all **non-production** gates pass. Blocked checks
typically include:

- `production_root_hard_deny`
- `execution_disabled`
- `gateway_disabled` or unsupported enablement
- Missing repository attestation for isolated drill

## Cutover checklist

```bash
hermes coo dispatch production cutover-check --ticket-id <ticket-id>
```

`cutover_ready=true` means isolated pilot fleet checks pass. It does **not** set
`production_execution_allowed=true`.

## Dashboard view

```bash
hermes coo dispatch gateway dashboard
```

Shows `signoff_ready`, `cutover_ready`, and `dashboard_health` together.

## Why production stays blocked

1. **Policy** — COO layer hard-denies production roots and execution flag.
2. **Gateway enabled** — Not a supported operator path; remains staged mock.
3. **Runner binding** — Production runner not bound for live execution.
4. **Repository2** — Attestation is read-only; pipeline execution not invoked.
5. **Operator safety** — Confirmation phrases and multi-gate approval required
   for any future execution enablement (out of scope for current operator docs).

## Operator interpretation

| Observation | Action |
| ----------- | ------ |
| `signoff_ready=false` | Run `production readiness`; fix failed checks |
| `cutover_ready=false` | Review `pilot fleet` per ticket |
| `BLOCKED` dashboard | See [Gateway_Runbook.md](Gateway_Runbook.md) |
| Attestation pass, execution denied | Expected — attestation ≠ execution permission |

## Related documents

- [Pilot_Runbook.md](Pilot_Runbook.md)
- [Gateway_Runbook.md](Gateway_Runbook.md)
- [Operator_Checklist.md](Operator_Checklist.md)
