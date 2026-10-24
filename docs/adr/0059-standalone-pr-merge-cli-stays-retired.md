# ADR-0059: Standalone PR-merge CLI stays retired

- Status: Accepted
- Date: 2026-10-23
- Tracks: #3358

## Context

PR #3340 preserved leftover check-run work from the old issue #2479 checkout;
issue #3358 asks for an integrate-or-retire decision. The preserved overlay
consists of three files:

- `hephaestus/github/pr_merge.py`
- `tests/unit/github/test_github_utils.py`
- `tests/unit/github/test_pr_merge.py`

The overlay was marked as a preservation draft (do-not-merge). After rebasing
every open PR onto `origin/main`, its remaining overlay was not integrated.

Key historical evidence:

- Merged PR #2693 (commit `3a41d6aa`, closes #2479) already delivered the exact
  requirement: require head-scoped check runs, paginate head check runs, verify
  unique paginated check runs, and remove the legacy commit-status fallback.
- PR #3158 (commit `920062de`, refactor(automation)!) intentionally deleted
  `hephaestus/github/pr_merge.py` as part of making the queue pipeline the sole
  automation owner (ADR-0050).
- On current HEAD, `test ! -f hephaestus/github/pr_merge.py` passes.
- The preserved overlay's `_fetch_commit_check_runs` fetches
  `check-runs?per_page=100` without pagination or a `total_count` check — this
  was flagged as a P1 required finding in PR #3340's own review.
- The overlay does not import on current main: it references
  `hephaestus.github.git_ops` (`git_branch_exists`, `git_push`,
  `git_remote_url`, `run_git`), but that module does not exist. Those helpers
  moved to `hephaestus/utils/git.py` (lines 264, 282, 297), covered by
  `tests/unit/github/test_git_ops.py`.
- No `pr_merge` console script exists in `pyproject.toml` `[project.scripts]`.

This is the third repeat of this decision cycle (#2479 → #2693 → #3340 → #3358).
A durable enumerable record closes the cycle.

## Decision

Do not re-integrate `hephaestus/github/pr_merge.py`,
`tests/unit/github/test_github_utils.py`, or
`tests/unit/github/test_pr_merge.py`.

The check-run aware merge capability stays queue-owned through the `merge_wait`
stage. The four current coverage points that supersede the overlay are:

1. Exact-head conditional squash merge →
   `hephaestus/automation/pipeline_github_merge_mutations.py:164`
   `merge_pr_if_head` (uses `-f sha={reviewed_sha}`, `-f merge_method=squash`
   at `pipeline_github_merge_mutations.py:218`).
2. Merge-queue handling → policy-first detection
   (`pipeline.merge_queue_required` in
   `hephaestus/automation/pipeline_github_merge_policy.py`) plus exact-head
   GraphQL admission `enqueue_pull_request_mutation(pull_request_id,
   reviewed_sha)` in `_enqueue_pr_if_head`
   (`pipeline_github_merge_mutations.py:116`). This supersedes the overlay's
   trial-and-error HTTP-405 detection (`_is_merge_queue_error`).
3. Required check-run evidence for the merge head →
   `hephaestus/automation/pipeline_github_required_checks.py`,
   `hephaestus/automation/github_api/checks.py` (`gh_pr_checks`,
   `checks.py:44`), `hephaestus/automation/pipeline_github_check_run_inventory.py`;
   enforced by the `merge_wait` stage
   (`hephaestus/automation/pipeline/stages/merge_wait.py:114`).
4. Merge-strategy selection (the overlay's `choose_merge_flag`) → policy
   `merge_method` in
   `hephaestus/automation/pipeline_github_merge_rules.py:54`.

## Alternatives considered

- **Integrate the overlay as a restored module** — rejected. It duplicates
  existing queue-owned behavior, regresses pagination (no `total_count` check),
  and depends on the stale `hephaestus.github.git_ops` import that no longer
  exists.
- **Salvage only `choose_merge_flag`** — rejected. The merge-strategy selection
  is already covered by `pipeline_github_merge_rules.py:54`.
- **Record the decision only in issue comments** — rejected. This is the third
  repeat of the same decision; a comment is not an enumerable, reviewable
  record. The structural guard `tests/unit/docs/test_adr_records.py` requires
  an ADR with Nygard sections (Context / Decision / Alternatives considered /
  Consequences), `- Status:` / `- Date:` lines, and bidirectional README index
  sync.

## Consequences

- `hephaestus/github/` keeps its current module set; the preserved overlay
  files are not added.
- The preservation branch `codex/preserve-2479-work-20261004` may be deleted
  after this ADR merges; the diff of closed PR #3340 stays reachable through
  GitHub pull-request refs, so no evidence is lost.
- Any future check-run aware merge change belongs in the queue pipeline
  (e.g., `merge_wait` stage, `pipeline_github_required_checks.py`,
  `pipeline_github_check_run_inventory.py`), not in a standalone CLI module.
- The ADR structural guard `test_adr_records.py` passes because this record
  follows the required format and the README index is updated.
