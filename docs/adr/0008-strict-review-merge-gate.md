# ADR-0008: strict_review as the single authority for auto-merge arming

- Status: Accepted
- Date: 2026-07-11
- Tracks: #2055, #2053, #2054

## Context

Before this change, `pr_review`'s own in-loop review verdict directly wrote
`state:implementation-go` AND armed auto-merge (`ctx.github.arm_auto_merge`)
in the same stage. That collapses "the code passed one automated review"
and "this exact commit is authorized to merge" into a single, self-graded
step: the reviewer that produces the verdict is also the actor that acts on
it, with no independent second check and no binding between the verdict and
the commit it was actually given at review time versus the one that ends up
merging. Issue #2054 (parent epic #2053) makes every automatic path fail
closed — no automatic caller may arm — which closes the immediate hole but
leaves the queue with no way to safely re-enable automation: a label alone
is an insufficient trust anchor because it can be stale (written for an
earlier commit), forged (any writer with issue-edit permission can add it),
or simply attached to the wrong PR head after a force-push.

## Decision

Insert `strict_review` as the ninth queue stage
(`pr_review -> strict_review -> ci -> merge_wait`) and make it the ONLY
automatic producer of `state:implementation-go`. Give `merge_wait` sole
authority to arm auto-merge, and make it re-validate the authorization
immediately before every arm attempt rather than trusting a label written at
some earlier point:

1. **Independent second reviewer.** `strict_review` runs a fresh, read-only
   agent session (`AgentJob(sandbox="read-only", ...)`) with no write or
   GitHub-mutation capability, re-deriving its own GO/NOGO verdict from the
   PR's CURRENT diff — it does not defer to `pr_review`'s verdict, and it
   cannot itself label, comment-mutate, or arm anything beyond posting its
   own read-only artifact and the durable GO/NOGO label.
2. **Head-bound authenticated artifact.** A GO verdict is durably published
   as a versioned PR-comment artifact (SHA-256 digest over
   `head_sha + verdict + body`, authored by the authenticated automation
   identity) BEFORE the `state:implementation-go` label is written — artifact
   precedes label, so a crash between the two never leaves a label standing
   without its proof. `merge_wait` re-reads this artifact — not the label —
   immediately before every arm attempt, and refuses to arm on any artifact
   that is missing, foreign-authored, digest-tampered, head-mismatched, or
   an authenticated NOGO.
3. **Single armer, fresh validation.** `pr_review` and `strict_review` both
   stop short of arming; `MergeWaitStage._arm` is the only method in the
   codebase that calls `arm_auto_merge` (enforced by an AST guard,
   `test_pipeline_architecture.test_only_merge_wait_calls_arm_auto_merge`),
   and it runs a PREPARE (re-read head + artifact) / ARM / CONFIRM
   (readback `autoMergeRequest`) sequence rather than a single write, so a
   race where the PR is merged or the head moves between validation and
   arming is reconciled through the existing MERGED dedupe path instead of
   silently double-arming or silently missing a state.
4. **Head-change revocation.** If the PR's head moves after a GO artifact
   was published (a new push landed), `strict_review` detects the mismatch
   on re-entry, durably clears the stale label, verifies auto-merge is
   actually disabled, and restarts review for the new head — an old
   artifact can never authorize a new commit.

## Alternatives considered

- **(a) Keep arming inside `pr_review`, just gate it on a stricter internal
  check.** Rejected: the actor and the checker are still the same stage, so
  there is no independent verification boundary — a bug or prompt-injected
  verdict in the single reviewer still directly authorizes a merge.
- **(b) Trust the `state:implementation-go` label alone at `merge_wait`.**
  Rejected: a label is a bare string with no cryptographic binding to a
  commit or an author; it survives force-pushes, can be re-applied by
  mistake or by a compromised token, and does not on its own prove which
  commit was reviewed.
- **(c) Make `strict_review` itself the armer (fold `merge_wait`'s job into
  it).** Rejected: `merge_wait` already owns the durable arming record, the
  post-merge `/learn` dedupe, and the DIRTY/BLOCKED resolution loop: moving
  arming there keeps ONE stage responsible for "is this PR mechanically
  ready and authorized to merge right now," re-validated at the moment of
  the actual mutation, rather than splitting that responsibility across two
  stages with a time gap between them.
- **(d) Sign the artifact with a cryptographic key instead of an
  authorship+digest check.** Rejected for this iteration: GitHub's own
  authenticated-identity guarantee (`gh_current_login()` on a
  repo-scoped/org-scoped token) plus a digest that binds the verdict to an
  exact head SHA already closes the practical spoofing/staleness gaps this
  ADR targets, without adding a separate key-management surface.

## Consequences

- **+ Independent verification**: no single stage can both grade its own
  work and act on that grade — `strict_review` grades, `merge_wait` acts,
  and each re-checks the other's durable output before proceeding.
- **+ Fail-closed by construction**: `strict_review_artifact()` returns
  `None` (never a substitute NOGO or stale GO) on any authentication
  failure, and every caller treats `None` as "no trusted authorization" —
  there is no code path where an ambiguous artifact read defaults to
  arming.
- **+ Race-safe arming**: the PREPARE/ARM/CONFIRM sequence means a PR that
  merges or moves its head during the arm attempt is reconciled through the
  existing MERGED dedupe rather than producing an inconsistent arm state.
- **− Extra review latency**: every PR now pays for a second, independent
  agent review pass before it can be armed, adding wall-clock time and
  token cost per PR beyond the existing `pr_review` loop.
- **− Additional session/token surface**: `strict_review` introduces its own
  per-head/per-attempt session-naming scheme (`strict_review_agent`) and a
  new `AgentJob.sandbox` field threaded through the worker pool — a second
  mechanism alongside `reviewer_agent`'s per-iteration sessions that callers
  must keep straight.
