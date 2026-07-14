# V1 Release Candidate Validation

Read-only validation record for the Hermes COO dispatch V1 release
candidate. No production code, CLI behavior, or test expectations were
changed to produce this document.

## RC status

**V1_RC_READY_WITH_WARNINGS**

| Field | Value |
| ----- | ----- |
| Validated HEAD | `68a310e5c` |
| Phase 15 pre-cutover baseline (rollback candidate) | `aec92da0c` |
| Tag candidate | `v1.0.0-rc.1` |
| Tag/push performed | No — read-only validation only |

## Why READY_WITH_WARNINGS and not READY

No BLOCKER-class finding exists: `production_execution_allowed=true` is
unreachable from any CLI path, `production_root_hard_deny=true` holds
everywhere, no code path executes the real Repository2 checkout, and the
full Phase 14–15J governed chain (activation → final sign-off → governed
cutover → window → permission → session → boundary → invocation →
authorization → runtime start → governed invoke → governed closure) was
exercised end-to-end with a live (non-mocked) run and produced
`CLOSURE_COMPLETED` with `correlation_valid=true`,
`consume_chain_complete=true`, `production_execution_allowed=false`, and
`original_repository2_execution_attempted=false`.

Two HIGH-severity warnings (below) prevented a plain READY verdict:
documentation coverage gap for Phase 14–15J, and an unverifiable slice of
the test suite due to a missing `pytest` dependency.

## Known Warnings

| Warning | Classification |
| ------- | -------------- |
| `docs/operator/` pre-dated Phase 14A and did not document the `activation`/`governed-cutover` CLI trees | **Resolved by this Phase (16B)** |
| pytest not installed; 231 of 471 files under `tests/hermes_cli/` cannot be collected by `unittest` | V1.1 backlog (environment/packaging issue) — none of the 231 files touch COO/production code, confirmed by direct search |
| `hermes_cli/coo_dispatch.py` is ~4,700 lines | V1.1 backlog (maintainability) — mitigated today by this documentation |
| Phase 14/15 `production_*.py` modules are individually 2,000–3,600 lines with duplicated TTL/identity/atomic-store boilerplate across ~15 near-identical stage modules | V1.1 backlog (maintainability); no correctness impact found |
| No cross-store transaction across the Phase 15 chain (each stage's artifact write is independently atomic, not atomic as a whole) | Mitigated by operational procedure — every stage cross-validates correlation ids against its predecessors, so a crash mid-chain fails closed at the next evaluation rather than silently proceeding |
| `production_root_hard_deny` constant is independently duplicated in `dispatch_pipeline_root_trust.py` (canonical) and `dispatch_executor_config.py` | V1.1 backlog (consolidate to single import); current values identical, both implementations correct |
| Manual window close required; automatic window/session closure not implemented | Mitigated by operational procedure — see [Operator_Checklist.md](Operator_Checklist.md#governed-cutover-chain-review-read-only-phase-1415) |
| Session close / invocation completion lifecycle not implemented | V1.1 backlog — explicitly out of V1 scope, see [V1_Scope_Freeze.md](V1_Scope_Freeze.md) |
| `python -m hermes_cli.coo_dispatch` has no `__main__` guard and produces no output when invoked this way | V1.1 backlog (packaging) — the documented operator entrypoint is `hermes coo dispatch ...`, not direct module invocation |

## Validation performed

### Git / repository

- HEAD `68a310e5c`, working tree clean, `git diff --check` clean
- No unresolved merge-conflict markers (one grep hit in
  `tests/tools/test_mcp_oauth_metadata.py` was a docstring section
  underline, confirmed not a real conflict)
- Last 10 commits are the sequential Phase 15A–15J `feat(production): ...`
  series; no anomalies

### Production safety invariants

- Zero non-test call sites set `force_production_execution_allowed=True`
  or pass `production_execution_allowed=True`
- `production_root_hard_deny` enforcement (`os.path.commonpath`
  containment check) verified intact wherever a real gate exists
- `governed_runtime_invoked` vs `isolated_mirror_runtime_invoked` /
  `phase14_runtime_invoked` separation confirmed live (both fields present
  and distinct on the same closure record)

### State chain

Live, non-mocked execution of the full Phase 14–15J chain reached
`CLOSURE_COMPLETED`. `subprocess.run`/`subprocess.Popen` were patched to
raise `AssertionError` for the duration of the run and were never invoked.

### CLI compatibility

- Full argparse tree walked programmatically; documented in
  [CLI_Command_Reference.md](CLI_Command_Reference.md)
- Legacy `production readiness`/`sign-off`/`cutover-check` commands
  unchanged in output and exit code
- `governed-cutover` and legacy `cutover-check` coexist without namespace
  collision (verified via `--help` at every level of the tree)
- No `invoke` or `closure` subcommand exists anywhere in the tree — Phase
  15I/15J CLI non-exposure confirmed

### Tests

| Suite | Result |
| ----- | ------ |
| `agent/coo/tests/` | 482/482 passed |
| `tests/hermes_cli/` | 3,897/4,128 passed; 231 collection errors, all `ModuleNotFoundError: No module named 'pytest'`, all in files unrelated to COO/production |
| `tests/plugins/test_discord_coo_*.py`, `tests/gateway/test_coo_approval_dispatch.py`, `tests/tools/test_coo_*.py` | 179/179 passed |

No result above is rounded up or described as "all passing" where a gap
exists — the 231 uncollected files are reported as uncollected, not as
failing or passing.

## Rollback guidance

If `68a310e5c` needs to be rolled back:

- Roll back to `aec92da0c` (last commit before Phase 15A) or an earlier
  known-good commit as appropriate.
- Append-only artifacts already written to disk (activation, cutover,
  window, permission, session, boundary, invocation, authorization,
  runtime-start, consume, and closure records) are **not** deleted by a
  code rollback and should not be deleted manually — they remain the audit
  trail for whatever activation/cutover chain was in progress.
- A code rollback does not require, and should not be paired with, any
  artifact cleanup.

## Related documents

- [V1_Scope_Freeze.md](V1_Scope_Freeze.md)
- [V1_Release_Notes.md](V1_Release_Notes.md)
- [Architecture_Overview.md](Architecture_Overview.md)
