# Pi Issue 2519 Report

- Evidence status: `incomplete`
- Fixture: `fix(utils): reject negative byte sizes`
- Run ID: `20260810T074700Z-live`
- Created: `2026-08-10T07:46:48Z`
- Pi version: `0.80.2`
- Pi binary: recorded privately in the run manifest
- Skill commands: `skill:advise`, `skill:learn`, `skill:pr-review`
- Inventory status: `ready`
- Inventory ready: `True`

## Verification Outcome

This is an incomplete, unverified partial capture, not an end-to-end Pi workflow
attestation. It is not closure evidence for #2519.

The only captured Pi command failed during planning. No isolated Pi worktree,
repository snapshot, successful test run, commit/PR creation, review, or handoff
evidence has been recorded.

Missing required acceptance evidence:

- A repository snapshot bound to the Pi run.
- Successful isolated Pi planning, implementation, tests, commit/PR creation,
  review, and handoff stage receipts.
- Pi/control comparison evidence for the same fixture, prompt, and recorded
  revision, and persisted success artifacts or failure behavior.
- Mnemosyne advise/learn delivery receipts bound to the Pi workflow.
- Publication attestation for the rendered report and runbook.

Host receipts or a control-provider run do not substitute for the missing
isolated Pi workflow evidence.

## Captured Commands

| Stage | Provider | Status | Returncode | Session evidence | Tool scopes | Skill calls |
| --- | --- | --- | --- | --- | --- | --- |
| planning | pi | failure | 1 | `019feaa3-88b4-76c6-84c0-858c63b4cb31` | read, grep | n/a |
| control | codex | unverified / unproven | claimed `0` (private manifest only) | none | n/a | n/a |

The Codex control result is unverified: no committed, report-bound control
transcript exists for independent re-execution. The available host receipts
cover linting, type checking, and unit tests only; they do not establish this
Codex invocation. A committed transcript bound to this report and reproducible
by an independent reviewer is required before this row can be marked successful.

## Snapshots

_No repository snapshots recorded yet._

## Defects

- Follow-up issue #2738: Fresh Pi console processes had no external adapter bootstrap

## Publication

- Runbook: `docs/runbooks/pi-e2e-2519.md`
- Report: `docs/pi-e2e-2519-report.md`
