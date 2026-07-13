# COO Dispatch Operator Documentation

Read-only operator guides for the Hermes COO dispatch, gateway, pilot, recovery,
and dashboard surfaces. These documents describe **existing** CLI behavior only.
They do not enable production execution.

## Documents

| Document | Purpose |
| -------- | ------- |
| [Dispatch_Runbook.md](Dispatch_Runbook.md) | End-to-end dispatch lifecycle and operator actions |
| [Gateway_Runbook.md](Gateway_Runbook.md) | Gateway state, readiness, pilot, audit, correlation, dashboard |
| [Recovery_Runbook.md](Recovery_Runbook.md) | Consume recovery, repair, locks, and manual operator flow |
| [Pilot_Runbook.md](Pilot_Runbook.md) | Isolated pilot, history, regression, trend, fleet, cutover |
| [Production_Signoff.md](Production_Signoff.md) | Why production remains blocked and sign-off gates |
| [Operator_Checklist.md](Operator_Checklist.md) | Daily, weekly, release, and incident checklists |
| [CLI_Command_Reference.md](CLI_Command_Reference.md) | Full `hermes coo dispatch` command tree |
| [Architecture_Overview.md](Architecture_Overview.md) | High-level flow from approval to dashboard |

## Safety policy

Operator documentation intentionally excludes secrets, credentials, confirmation
phrases, unlock identifiers, pipeline roots, repository paths, raw process
output, and environment details. Use opaque IDs and CLI summaries only.

## Production warning

`production_execution_allowed` remains **false** by policy. Gateway **enabled**
state is not supported for live production dispatch. All pilot and gateway mock
paths run under `isolated_gateway_mock` scope unless explicitly documented
otherwise.
