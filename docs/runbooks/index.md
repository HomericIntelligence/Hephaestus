# Operations Runbooks

Operator recovery procedures for the `hephaestus.automation` pipeline. Start
here when the automation loop, a worktree, the CI driver, or a Claude stage
needs hands-on recovery.

## Runbooks

| Runbook | Use when |
| ------- | -------- |
| [Automation loop crashed mid-issue](automation-loop-crash.md) | The `hephaestus-automation-loop` process died or a phase timed out and you need to resume safely. |
| [Recover a corrupted worktree state](corrupted-worktree.md) | An issue's `build/.worktrees/issue-<N>` worktree is dirty, abandoned, or blocking a clean re-run. |
| [CI-driver stall (green-but-BLOCKED)](ci-driver-stall.md) | The CI driver exits cleanly each loop but PRs stay armed, green, and un-mergeable (`mergeStateStatus == BLOCKED`). |
| [Claude quota exhausted (429)](claude-quota-exhausted.md) | A stage emits `Verdict: ERROR` and the issue is left unlabeled because the Claude API quota / session limit was hit. |
| [Reviving a state:skip-labeled issue](state-skip-revival.md) | An issue was labeled `state:skip` after automation already started work on it (planned or opened a PR) and you want to resume driving it. |
| [No silent failures](no-silent-failures.md) | Policy reference: why `\|\| true`, `continue-on-error`, and advisory `::warning::` are forbidden, and how to fix a tripped hook. |

## Before you start

- **Pipeline stages** — the stage → module → console-script mapping lives in
  [`../../AGENTS.md`](../../AGENTS.md). Use it to identify which module owns the
  behavior you are recovering.
- **PR & state-label policy** — the PR policy (signed commits, `Closes #N`,
  auto-merge gating) lives in [`../../CLAUDE.md`](../../CLAUDE.md).

## State-label reference

The pipeline drives every issue through `state:*` labels (defined in
`hephaestus/automation/state_labels.py`). This table is a manual quick-reference
copy — the module is the source of truth.

| Label | Meaning |
| ----- | ------- |
| `state:needs-plan` | Issue is queued for the planner (also the implicit state when no `state:*` label is present). |
| `state:plan-go` | Plan reviewed and approved; ready for implementation. |
| `state:plan-no-go` | Plan reviewed and rejected; needs re-planning. |
| `state:implementation-go` | Implementation reviewed and approved; PR may arm auto-merge. |
| `state:implementation-no-go` | Implementation reviewed and rejected; needs re-work. |
| `state:skip` | Work item taken out of the loop entirely — operator-applied, auto-applied when the review loop exhausts its budget without a GO, or applied to epics before exclusion. Independent of all other state labels. See [Reviving a state:skip-labeled issue](state-skip-revival.md) to safely clear it. |
