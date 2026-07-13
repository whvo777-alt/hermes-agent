# Recommended Action Mapping

Fixed mapping from `recommended_action` codes to in-repo runbook references.
Used by CLI `operator guidance`, Discord operational status, dashboard, and
correlation output. No shell commands or filesystem paths are emitted.

## Format

| Field | Example |
| ----- | ------- |
| `runbook_ref` | `Recovery_Runbook#manual-recovery` |
| `runbook_name` | `Recovery_Runbook` |
| `runbook_section` | `manual-recovery` |

Resolve guidance:

```bash
hermes coo dispatch operator guidance --recommended-action <code>
```

## Core operator actions

| `recommended_action` | `runbook_ref` | Summary |
| -------------------- | ------------- | ------- |
| `no_action_required` | `Gateway_Runbook#recommended-action-codes` | No intervention; continue monitoring |
| `run_gateway_pilot_dry_run` | `Gateway_Runbook#gateway-pilot` | Dry-run preflight before live mock |
| `run_gateway_mock_pilot` | `Gateway_Runbook#gateway-pilot` | Staged mock pilot when regression allows |
| `collect_more_history` | `Pilot_Runbook#history` | Collect more pilot history |
| `collect_more_pilot_history` | `Pilot_Runbook#history` | Alias for pilot history collection |
| `inspect_latest_failure` | `Operator_Checklist#pilot-incident` | Review latest pilot failure |
| `investigate_recent_failure` | `Operator_Checklist#pilot-incident` | Alias for recent failure review |
| `inspect_missing_evidence` | `Gateway_Runbook#correlation-explorer` | Inspect evidence or audit gap |
| `resolve_recovery_required` | `Recovery_Runbook#manual-recovery` | Manual recovery for consume pair |
| `resolve_recovery_issue` | `Recovery_Runbook#manual-recovery` | Alias for recovery resolution |
| `resolve_correlation_mismatch` | `Gateway_Runbook#correlation-explorer` | Resolve ID mismatch across stores |
| `resolve_regression_failure` | `Pilot_Runbook#regression` | Resolve pilot regression before mock |
| `stage_gateway` | `Gateway_Runbook#gateway-state` | Stage gateway before mock pilot |
| `maintain_production_block` | `Production_Signoff#why-production-stays-blocked` | Keep production execution disabled |

## Extended codes

| `recommended_action` | `runbook_ref` | Summary |
| -------------------- | ------------- | ------- |
| `inspect_execution_failure` | `Gateway_Runbook#correlation-explorer` | Inspect execution failure in chain |
| `inspect_consume_state` | `Recovery_Runbook#when-recovery-required` | Review consume state before repair |
| `inspect_regression` | `Operator_Checklist#gateway-incident` | Inspect regression drift |
| `inspect_consume_drift` | `Recovery_Runbook#correlation-during-recovery` | Inspect consume drift between requests |
| `provide_more_specific_id` | `Gateway_Runbook#correlation-explorer` | Provide a more specific correlation id |
| `provide_same_ticket_requests` | `Gateway_Runbook#correlation-explorer` | Compare same-ticket requests only |
| `request_not_found` | `CLI_Command_Reference#gateway` | Verify gateway request id |

## Safety

- `production_execution_allowed` remains **false** for all codes.
- Unknown codes fail closed in `operator guidance` (exit code `1`).
- Dashboard and correlation append guidance only for known codes.

## Related documents

- [Gateway_Runbook.md](Gateway_Runbook.md)
- [Recovery_Runbook.md](Recovery_Runbook.md)
- [Pilot_Runbook.md](Pilot_Runbook.md)
- [Production_Signoff.md](Production_Signoff.md)
- [Operator_Checklist.md](Operator_Checklist.md)
- [CLI_Command_Reference.md](CLI_Command_Reference.md)
