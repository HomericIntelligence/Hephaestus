# Source-reading agent workspace inventory

Issue #2764 establishes the following closed inventory. `impl` means
`build/.worktrees/auto-<#>-impl`; `review` means the detached
`build/.worktrees/auto-<#>-review`. A compact turn is transcript-only and does
not receive source-reading tools.

| Stage / call | Job | Source capability | Lane |
|---|---|---:|---|
| planning advise | `AthenaSkillJob(advise)` | yes | review |
| planning plan | `AgentJob(planner)` | yes | review |
| plan review | `AgentJob(plan_reviewer)` | yes | review |
| plan amendment | `AgentJob(planner)` | yes | review |
| implementation dirty-state inspection | `AgentJob(implementer)` | yes | impl |
| implementation advise | `AthenaSkillJob(advise)` | yes | impl |
| implementation / remediation | `AgentJob(implementer)` | yes | impl |
| rebase conflict resolution | `AgentJob(implementer)` | yes | impl |
| test remediation | `AgentJob(implementer)` | yes | impl |
| PR review analysis | `AgentJob(pr_reviewer)` | yes | review |
| PR comment validation | `AgentJob(pr_reviewer)` | yes | review |
| reviewer / writer compaction | `CompactJob` | no | session-only operation |
| approved-plan / post-merge learning | `AthenaSkillJob(learn)` | yes | impl |

The worker boundary rejects every source-capable raw job aimed at a primary
checkout (`.git` directory). Typed jobs additionally validate repository
ownership, deterministic path, receipt generation, exact `HEAD`, cleanliness,
and branch/detached state while holding the lane lock for the provider call.
Host-owned Athena work uses the same validation and lease path and never gains
an exception merely because it bypasses an agent harness.

## Failed writer handoffs

The source manager records failed implementation handoffs under the lane lock.
The `<issue>-impl-terminal.json` file contains a failure snapshot. It does not
supply ownership or permission to change a checkout. The worker transfers only
the snapshot filename and content digest to the finished stage.

Before it records a wave result or ledger result, the finished stage verifies
three records: the terminal snapshot, the complete transition journal, and the
exact source receipt bytes. A missing or changed record gives the same
`source_workspace_recovery_receipt_invalid` result to both stores. An unchanged
incomplete transition retains its phase, cause, and exact retry action.

Failed handoffs preserve the worktree, source receipt, local branches, journal,
and direct reservation. This rule also applies when the worktree path is absent.
A failed snapshot write or readback cannot permit cleanup. A subsequent retry
must use the source manager and its existing journal recovery checks.

A checkout that differs from its source receipt needs ownership evidence.
Branch names, shared ancestry, and diagnostic events do not supply that evidence.
Without a transition journal, the manager preserves the checkout and reports the
recorded and observed branch and revision. The operator must supply the missing
ownership record or separately reviewed recovery instructions. The terminal
snapshot does not permit receipt editing or automatic adoption.
