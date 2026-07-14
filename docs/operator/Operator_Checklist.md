# Operator Checklist

Repeatable checklists for COO dispatch operators. All steps use read-only commands
unless explicitly marked as gated mutation.

## Daily

- [ ] `hermes coo dispatch gateway dashboard` — note `dashboard_health`
- [ ] `hermes coo dispatch pilot regression` — confirm not `FAIL`
- [ ] Review Discord operational status if gateway sessions active
- [ ] Scan `recommended_action` on dashboard; escalate `BLOCKED`
- [ ] Verify no open `recovery_required` tickets:
  `hermes coo dispatch consume recovery --ticket-id <ticket-id> --confirmation-id <confirmation-id>`

## Weekly

- [ ] `hermes coo dispatch production sign-off`
- [ ] `hermes coo dispatch production cutover-check --ticket-id <ticket-id>`
- [ ] `hermes coo dispatch pilot runbook` — review trend status
- [ ] `hermes coo dispatch pilot fleet --ticket-id <ticket-id>`
- [ ] `hermes coo dispatch gateway correlation diff` on oldest vs newest request per active ticket
- [ ] `hermes coo dispatch binding status` — runner binding state

## Release (staged gateway mock)

- [ ] `gateway status` — `staged`, not `enabled`
- [ ] `gateway readiness` — no failed checks
- [ ] `production sign-off` — document blocked checks (expected production denials)
- [ ] `pilot regression` — `PASS` or acceptable `WARN`
- [ ] `gateway pilot run --dry-run` for release ticket
- [ ] `gateway audit show` on release gateway request id
- [ ] `gateway dashboard --ticket-id <ticket-id>` — target `HEALTHY` or documented `DEGRADED`

<!-- anchor: pilot-incident -->
## Pilot incident

- [ ] `gateway correlation show` with failing opaque id
- [ ] `gateway audit show --gateway-request-id <id>`
- [ ] `pilot history show --pilot-attempt-id <id>`
- [ ] `evidence show --execution-attempt-id <id>`
- [ ] `audit show --dispatch-run-id <id>`
- [ ] Record `recommended_action` code from correlation output
- [ ] Do **not** re-run live mock until regression reviewed

## Recovery incident

- [ ] `consume status` + `consume recovery`
- [ ] `consume repair lock status`
- [ ] `operator runbook --ticket-id <ticket-id> --confirmation-id <confirmation-id>`
- [ ] `consume repair dry-run` before any apply
- [ ] `consume repair audit list` after apply
- [ ] `gateway dashboard` — confirm recovery cleared

<!-- anchor: gateway-incident -->
## Gateway incident

- [ ] `gateway status` + `gateway facade`
- [ ] `gateway readiness`
- [ ] `gateway dashboard --ticket-id <ticket-id> --session-id <session-id>`
- [ ] `gateway correlation show` — verify `correlation_valid`
- [ ] If mismatch: `gateway correlation diff` between known-good and suspect request
- [ ] Escalate `correlation_mismatch` and `BLOCKED` health

## Production review (read-only)

- [ ] `production readiness`
- [ ] `production sign-off`
- [ ] `repository attest` — read-only only
- [ ] Confirm `production_execution_allowed=false` in all summaries
- [ ] Confirm `production_root_hard_deny=true`
- [ ] Document why each blocked check is intentional

<!-- anchor: governed-cutover-review -->
## Governed cutover chain review (read-only, Phase 14/15)

- [ ] `production activation status` — confirm expected lifecycle state
- [ ] `production governed-cutover status` — confirm contract present/valid
- [ ] `production governed-cutover window status` — **`WINDOW_OPEN` is not
  execution permission**; it only means the maintenance window is open
- [ ] `production governed-cutover permission status` /
  `... session status` / `... runtime-boundary status` /
  `... runtime-invocation status` / `... execution-authorization status` /
  `... runtime-start status` — walk the chain in order, confirm no
  unexpected `consumed`/`revoked` state
- [ ] Confirm `runtime_started=true` (if present) is read as "contract
  exists", never as "subprocess ran"
- [ ] If window is still `WINDOW_OPEN` after the operation is complete:
  close it manually — `production governed-cutover window close` (V1 does
  **not** auto-close windows)
- [ ] Session close and invocation-completion lifecycle are **out of V1
  scope** — do not expect an automated transition; see
  [V1_Scope_Freeze.md](V1_Scope_Freeze.md)

## Documentation

- [ ] [Dispatch_Runbook.md](Dispatch_Runbook.md) — lifecycle reference
- [ ] [CLI_Command_Reference.md](CLI_Command_Reference.md) — command lookup
- [ ] [Architecture_Overview.md](Architecture_Overview.md) — onboarding
- [ ] [V1_Scope_Freeze.md](V1_Scope_Freeze.md) — scope boundaries
- [ ] [Recovery_Runbook.md](Recovery_Runbook.md#phase-15-consume-records) — Phase 15 partial-consume/replay guidance
