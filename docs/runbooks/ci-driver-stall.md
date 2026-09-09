# Runbook: Queue Merge Stall

Use this runbook when a PR does not advance through `pr_review` or
`merge_wait`. A `state:implementation-go` label records implementation
eligibility. Before each merge request, the queue also requires review proof
from the current process, complete thread state, and passing required checks
for that exact head.

The default merge request budget is five. Use `--merge-attempts` to change it.
Readiness waits use `--poll-max-wait`, with a default of 1,200 seconds for each
fresh reviewed-head proof. A readiness wait does not spend a merge request
and does not grant merge authority.

## Inspect the current state

Read the current PR identity and native auto-merge state:

```bash
gh pr view <N> --json state,headRefOid,baseRefName,autoMergeRequest,labels
```

An existing `autoMergeRequest` belongs to an external owner. The queue does not
change it. An incomplete or unreadable response also blocks admission. Resolve
the ownership or read failure before another queue run.

Inspect checks for one captured head:

```bash
PR_HEAD="$(gh pr view <N> --json headRefOid --jq '.headRefOid')"
REPOSITORY="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')"
gh api "repos/${REPOSITORY}/commits/${PR_HEAD}/check-runs?per_page=100" \
  --paginate \
  --jq '{total_count, check_runs: [.check_runs[] | {name, head_sha, status, conclusion}]}'
gh api "repos/${REPOSITORY}/commits/${PR_HEAD}/status?per_page=100" \
  --paginate \
  --jq '{sha, total_count, statuses: [.statuses[] | {id, context, state}]}'
```

These reads are diagnostic evidence. The queue still performs its own complete
reads and policy checks. Each required Check Run must match the captured head
and finish with `success`, `neutral`, or `skipped`. A required commit status
must have `success`. A context bound to a GitHub App needs a Check Run from
that exact application. If a context occurs in both sources, both must pass.

The queue compares two complete paginated snapshots from each source. Missing,
changed, malformed, or incomplete evidence blocks the request. A later head
cannot satisfy checks for the reviewed head.

Inspect the queue log for the affected repository, item, and stage. JSONL
records are diagnostics; they are not a queue snapshot or a recovery journal.
A full stage queue indicates backpressure. A completion-channel saturation
message indicates an internal fault and requires a stopped run and inspection.

## Choose the recovery path

| Observed condition | Required action |
| --- | --- |
| Review proof is missing after restart | Run fresh review through the queue. A label or recovered audit receipt cannot restore this proof. |
| The current head differs from the reviewed head | Obtain review and verification for the new head. |
| Required checks are pending or failed | Repair or wait for those exact-head checks. |
| Review threads remain open | Let implementation provide replies, then let the reviewer validate and resolve them. |
| A reply or publication result is uncertain | Preserve the current journal, prepared commit, and worktree. Let the queue reconcile their exact identities before another attempt. |
| Host verification fails | Inspect the exact-head diagnostic. Repair the source failure or the host boundary before review. |
| GraphQL quota is low | Let the coordinator timer wait for the reset. Do not start a second owner. |
| A scope-expansion child is open | Complete that dependency. The source PR remains blocked. |

The implementation stage must settle or classify every actionable reply
handoff before it returns to review. Armed format 2 and remediation format 3
records remain supported. Old format 1 records have no conversion path.
Visibility retries are bounded. When they are exhausted, durable evidence
remains available for inspection.

Normal host verification uses the supported macOS boundary or the Linux
Pyxis/Enroot boundary. A missing image, authority, quota-backed scratch area,
or runtime cannot become a passing skip. There is no target-specific PR
exception. See [host verification](../architecture.md#55-pr-review).

From the repository checkout, run one scoped queue pass:

```bash
uv run hephaestus-automation-loop --prs <N> --loops 1 --max-workers 1
```

Use the full command with a contiguous stage scope when only part of the
pipeline is required:

```bash
uv run hephaestus-automation-loop --prs <N> \
  --stages pr_review,merge_wait --merge-attempts 5 --loops 1 --max-workers 1
```

For each merge request, `merge_wait` repeats open-PR, `main` base, unarmed
state, exclusive GO label, current-process proof, thread, and required-check
gates. It reads the effective classic and ruleset protection. A required merge
queue uses exact-head GraphQL admission. Otherwise, direct REST merge requires
strict-update protection from a source that the current actor cannot bypass.
The queue does not change native auto-merge or use an administrator bypass.

After queue admission or an uncertain server response, inspect the resulting
server lifecycle. Do not repeat a mutation only because its response was lost.
A second user or a marked `APPROVED` review is not a queue merge requirement.

## Cutover and rollback

This is a breaking migration. The supported queue commands are
`hephaestus-automation-loop`, `hephaestus-plan-issues`,
`hephaestus-implement-issues`, and `hephaestus-review-prs`. They share one
parser. Removed commands, option aliases, and retired records have no automatic
conversion path.

1. Stop the old coordinators. Drain active work or park it with its recovery
   evidence before deployment.
2. Preserve current journals, source ownership records, prepared commits,
   worktrees, learning claims, and issue-wave checkpoints. Keep unresolved
   historical effects for inspection; do not discard or replay them.
3. Start the retained queue command with an explicit repository and item scope.
   Inspect the recovery result before you expand the scope.
4. Use one coordinator version for each state directory. Do not run old and
   new owners against the same records.

Before rollback, stop the current coordinator and inspect all possible effects
produced after cutover. Restoring the old code does not prove that replay is
safe. Keep active journals and local source evidence until each unresolved
operation has a known outcome. A missing current learning intent does not
permit the queue to infer one from a historical merge.

## See also

- [Queue cutover decision](../adr/0048-queue-owned-automation-cutover.md)
- [Automation loop crashed mid-issue](automation-loop-crash.md)
- [Queue architecture](../architecture.md)
- [PR and state-label policy](../../AGENTS.md)
