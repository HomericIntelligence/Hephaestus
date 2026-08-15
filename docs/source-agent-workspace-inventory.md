# Source-reading agent workspace inventory

Issue #2764 establishes the following closed inventory. `impl` means
`build/.worktrees/auto-<#>-impl`; `review` means the detached
`build/.worktrees/auto-<#>-review`. A compact turn is transcript-only and does
not receive source-reading tools.

| Stage / call | Job | Source capability | Lane |
|---|---|---:|---|
| planning advise | `AthenaSkillJob(advise)` | yes | impl |
| planning plan | `AgentJob(planner)` | yes | impl |
| plan review | `AgentJob(plan_reviewer)` | yes | review |
| plan amendment | `AgentJob(planner)` | yes | impl |
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
