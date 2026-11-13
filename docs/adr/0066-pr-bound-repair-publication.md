# ADR-0066: PR-bound repair publication uses receipts, namespaces, and exact leases

- Status: Accepted
- Date: 2026-11-13
- Tracks: #2954

## Context

Workflow skills repair pull requests. The portable helper
`hephaestus-prepare-worktree` creates an isolated worktree at an attested
start point, but it has no publication authority: any caller can push any
history to any branch. An operator incident showed the risk. A repair agent
built its four-file change on top of another pull request's head that carried
105 unrelated file changes. Publishing that history as fast-forward would
have merged the unrelated parent into the target branch under the repair's
message.

The failure mode is structural. A generic worktree helper does not know which
pull request the repair belongs to, so it cannot answer the two questions a
publication must answer: is this history related to the reviewed pull
request, and is the remote head still the head the repair observed?

Two existing controls bound the risk without closing it. The review pipeline
publishes only through the queue, and branch protection requires CI checks.
Neither stops a worktree skill from pushing a contaminated head that still
passes CI, because the contamination is correct-by-construction content.

## Decision

1. `hephaestus/github/skill_pr_worktrees.py` owns two Provisional console
   scripts, `hephaestus-prepare-pr-worktree` and
   `hephaestus-publish-pr-worktree`. Each repair binds to one observed pull
   request through a strict schema-version-1 preparation receipt that records
   the target repository, number, canonical URL, base repository, branch, and
   tip, head repository, branch, and tip, creation mode, exact start object,
   dependency identity, initial census, source configuration digest,
   namespace identity, worktree identity, and expected remote head. The
   receipt is named by the digest of its immutable fields and is validated on
   every read.
2. All new refs, the local branch, and the worktree live in a receipt-owned
   bare Git namespace below `<repository-root>/build/`. The source checkout
   is read-only: preparation and publication change no source refs. The
   namespace carries a manifest with its directory identity, configuration
   digest, and permitted refs, revalidated before every Git process launch.
3. Publication is receipt-only. The worktree, namespace, local branch,
   remote repository, remote branch, and expected remote head come from the
   receipt; the grammar has no caller-supplied destination. The push uses
   `--force-with-lease=refs/heads/<branch>:<observed-head-oid>` against the
   canonical https URL of the receipt's head repository, so a moved remote
   head fails the push instead of being overwritten.
4. Publication holds the receipt lock through preflight and outcome-record
   updates. Before a push it revalidates the live target (and, for `stacked`
   mode, the dependency), reads a fresh bounded census, fetches each live
   sibling head into a private ref, and rejects a candidate that contains any
   unauthorized sibling ancestry. It then reads the target and dependency
   again, requires the exact remote head tip, and only then launches the
   push through `run_git(pre_launch=...)`.
5. Outcomes are classified as `published`, `reconciled`,
   `temporary-failure`, `rejected`, or `unknown` from an authoritative
   post-push readback. An `unknown` record blocks an immediate retry; the
   next call reconciles by readback and may retry only when the live state
   still matches the receipt exactly. There is no automatic rollback after
   confirmed publication.
6. The trusted remote Git implementation moves from
   `hephaestus.automation.remote_git` to `hephaestus.github.remote_git`. The
   old module stays as an identity-preserving compatibility export, keeping
   the `automation → library` direction from ADR-0001.
7. The process adapters gain the seams the boundary needs:
   `run_subprocess(pre_launch=...)` and `run_git(pre_launch=...)` revalidate
   local state immediately before each child launch without starting a child
   on failure, and `gh_call(executable=...)` accepts the default literal
   `gh` or one caller-trusted absolute path from
   `trusted_gh_executable()`.
8. The Myrmidon contract gains the integration-root rule: one integration
   revision produces one integrated result; each independent pull request
   gets its own receipt, namespace, worktree, integration root, and
   publication call; dependency ancestry is admitted only through explicit
   `stacked` mode.

## Alternatives considered

- **Add publication authority to `hephaestus-prepare-worktree`.** The helper
  is intentionally start-point-only and is used by paths that never push.
  Giving it pull request publication authority would widen a trusted surface
  that many callers share. A separate receipt-bound pair keeps the
  unrestricted helper unchanged.
- **Rely on `--force-with-lease` alone, without the receipt and censuses.**
  The lease stops a stale push but not a contaminated one: a wrong parent
  that the repair agent genuinely observed still passes the lease when the
  remote head has not moved. The receipt binds the repair to one pull
  request, and the sibling ancestry check rejects wrong parents before any
  write.
- **Publish through the merge queue instead of a push.** The merge queue
  closes a pull request after review; it cannot publish a repair to the pull
  request's own head while review is in progress. A lease push to the head
  branch is the only write that matches the repair workflow.
- **Roll back automatically after a confirmed publication.** A rollback push
  is itself an external write that can race with new review activity. The
  contract instead requires a new receipt that binds the newly observed head,
  so every correction is itself receipt-bound.

## Consequences

- Every repair publication is auditable: the receipt, the namespace manifest,
  and the outcome record under `<state-root>/` name the exact observed pull
  request state, the candidate, and the classified result.
- The exact lease makes a moved remote head a server-enforced rejection, not
  a local opinion. Publication never force-pushes over an unreviewed head.
- The double census and double live-read add four GitHub reads per
  publication. This is the cost of detecting drift that happens while the
  census itself runs, and it is bounded by the same page limits as one
  census.
- An `unknown` outcome leaves an outcome record that blocks silent retries.
  Recovery is explicit and revalidated, which trades one extra operator step
  for no unbounded push attempts.
- The moved `remote_git` module keeps its public names through the
  automation compatibility export. In-repository consumers and their test
  patches point at the library path; a revert of this ADR is one revert of
  the move.
