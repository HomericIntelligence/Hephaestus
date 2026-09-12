# Operations Runbooks

Use these procedures to recover the `hephaestus.automation` pipeline.
Start here when the automation loop, a worktree, `merge_wait`, or an agent job
needs operator action.

## Runbooks

| Runbook | Use when |
| ------- | -------- |
| [Automation loop crashed mid-issue](automation-loop-crash.md) | The `hephaestus-automation-loop` process died or a phase timed out and you need to resume safely. |
| [Recover a corrupted worktree state](corrupted-worktree.md) | An issue's `build/.worktrees/issue-<N>` worktree is dirty, abandoned, or blocking a clean re-run. |
| [Queue merge stall](ci-driver-stall.md) | A PR with loop-owned `state:implementation-go` remains blocked. |
| [Claude quota exhausted (429)](claude-quota-exhausted.md) | A stage reports a 429 quota/session-limit infrastructure failure and the issue remains unlabeled. |
| [Reviving a state:skip-labeled issue](state-skip-revival.md) | An issue was labeled `state:skip` after automation already started work on it (planned or opened a PR) and you want to resume driving it. |
| [Recovering an implementation-blocked issue](implementation-blocked-recovery.md) | An implementation run produced no commit and a human must choose the next action. |
| [Pi issue #2519 evidence run](pi-e2e-2519.md) | You need to reproduce the live Pi/Codex conformance evidence run, regenerate the report, or re-attest the publication artifacts. |
| [Pi rollout and recovery](pi-rollout.md) | You need to enable, verify, omit, or recover the managed Pi provider. |
| [No silent failures](no-silent-failures.md) | Policy reference: why `\|\| true`, `continue-on-error`, and advisory `::warning::` are forbidden, and how to fix a tripped hook. |
| [Backup and disaster recovery](backup-restore.md) | You need to back up or restore `build/.issue_implementer` state, or rebuild a lost workstation end-to-end (policy: ADR-0013). |
| [Recover the local CI Podman machine](local-ci-podman.md) | The macOS local CI runner cannot connect to Podman and uses the native fallback. |
| [Compacting issue timelines](compact-issue-timelines.md) | An upgraded release appended plan/review history to linked issues and you want to migrate that history off the issue timeline. |

## Before you start

- **Pipeline stages** — the stage → module → console-script mapping lives in
  [`../../AGENTS.md`](../../AGENTS.md). Use it to identify which module owns the
  behavior you are recovering.
- **PR and state-label policy** — see [`../../AGENTS.md`](../../AGENTS.md) for
  signed commits, `Closes #N`, and merge authorization for the exact reviewed head.

## State-label reference

The pipeline drives every issue through `state:*` labels (defined in
`hephaestus/automation/state_labels.py`). This table is a manual quick-reference
copy — the module is the source of truth.

| Label | Meaning |
| ----- | ------- |
| `state:needs-plan` | Issue is queued for the planner (also the implicit state when no `state:*` label is present). |
| `state:plan-go` | Plan reviewed and approved; ready for implementation. |
| `state:plan-no-go` | Plan reviewed and rejected; needs re-planning. |
| `state:plan-blocked` | Automation is stopped pending a stated decision or dependency. Comments do not resume it. After resolving the block, an external actor must replace this label with exactly one next plan-state label. |
| `state:blocked` | Automation is stopped pending an external dependency or operator action. |
| `state:implementation-go` | Automated implementation eligibility after structural audit and fresh exact-head facts. Merge wait separately requires its current-process reviewed-head proof and complete passing required status evidence for the exact head. |
| `state:implementation-no-go` | Implementation reviewed and rejected; needs re-work. |
| `state:skip` | Work item taken out of the loop entirely — operator-applied, auto-applied when the review loop exhausts its budget without a GO, or applied to epics before exclusion. Independent of all other state labels. See [Reviving a state:skip-labeled issue](state-skip-revival.md) to safely clear it. |
| `state:implementation-blocked` | Implementation produced no commit. The issue is excluded until a human chooses an action. It can coexist with `state:plan-go`; see [Recovering an implementation-blocked issue](implementation-blocked-recovery.md). |
