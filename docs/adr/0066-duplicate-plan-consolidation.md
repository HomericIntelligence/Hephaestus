# ADR-0066: Operator-authorized consolidation of duplicate canonical plan records

- Status: Proposed
- Date: 2026-11-12
- Tracks: #3307

## Context

Two tools can each publish one canonical plan/review pair on the same issue.
Both tools already recognize the shared `HomericIntelligence` markers and the
legacy Athena and Hephaestus aliases. When two qualifying pairs coexist, the
automation pipeline has no rule for which pair is current: the newest alias
is patched in place, review identity is never selected by age, tool name,
label, or a historical GO, and the round stalls on the identity conflict.
No existing mechanism supplies an explicit reconciliation, and comment
deletion stays outside the automation contract.

The recorded case (Comet #705) holds two same-actor pairs: one pair under
the legacy `athena:*` aliases and one pair under the shared markers. Each
pair carries its own review, so selecting one comment silently drops the
other pair's review evidence.

## Decision

1. Reconciliation of duplicate canonical pairs is operator-authorized only.
   The operator names the exact keep and supersede comment IDs and supplies
   the merged plan and review content. No code path selects a winner by age,
   tool name, label, or a historical GO.
2. One provenance header seals the result inside the consolidated plan
   comment: `HomericIntelligence:plan-consolidation` with the issue, the
   requirements digest, the epoch, and every source record (role, comment
   id, author, marker alias tag, content digest). The header is a strict
   fullmatch record; a journal read that cannot parse a header-shaped line
   fails closed.
3. Unknown `HomericIntelligence:`-namespaced header lines inside a plan or
   review comment are inert metadata. A conforming cross-tool reader
   tolerates them without a code change, exactly like the existing
   `revision:` and `prior-plan-fingerprints:` header lines.
4. Every superseded comment survives as an immutable history archive before
   any delete. The archive marker carries the superseded comment's own
   revision, so archive identity denotes the retired source, and the source
   body is wrapped in a code fence that keeps its planning markers invisible
   to marker readers. Two superseded sources of one role that share one
   revision share one marker; the planner aborts up front instead of writing
   an exact-replay conflict.
5. Pre-delete validation reads the journal only through the pure planning
   role inventory (foreign claims, coverage, digest drift). The
   duplicate-rejecting validator applies only after the deletes, inside the
   guarded actor-owned upsert, on a journal that holds exactly one owned
   claim per role.
6. The executor order is crash-safe: archives, drift re-verification,
   deletes, the canonical-content upgrade, then the label transition. Every
   write is confirmed by an exact read-back. An unconfirmed delete stops the
   run and names the superseded comments still to confirm; a re-run retries
   only unconfirmed work, and a completed replay performs no mutation.
7. The consolidated epoch is one past the highest source revision, and a
   historical GO label becomes `state:plan-no-go`. The merged review content
   must not carry a terminal state token, so the loop always runs a fresh
   independent plan review; a historical GO is never transferred to
   different content.
8. New writes use only the shared markers. Legacy aliases stay read-only so
   existing actor-owned comments can be upgraded in place. Foreign records
   are never mutated; a foreign plan/review claim refuses the consolidation
   outright.
9. The cross-tool agreement surface is the shared marker contract plus the
   reader-tolerance rule in decision 3. The companion deliverable on the
   Athena side documents the `plan-consolidation` header family in its
   issue-planning contract, and the Hephaestus PR links that companion PR.

## Alternatives considered

- Automatic winner selection by recency. Rejected: the newest comment can
  predate later migration and scoped-identity requirements, and review
  evidence from the superseded pair would be dropped silently.
- A semantic merge agent role. Rejected: it adds a new agent surface and
  prompt-fencing boundary for a case the operator must decide anyway; the
  tool binds the operator's merged content instead of authoring it.
- A generic comment-deletion command. Rejected: deletion targets stay
  operator-named, archive-verified, owned comments, so no general deletion
  surface exists.

## Consequences

- The provenance header and the archives are durable records. A later
  consolidation of the same issue earns a new epoch; an exact replay of the
  same command is idempotent.
- `journal_snapshot` and the timeline compactor preserve the provenance
  header through renders, so compaction and context projection cannot drop
  the binding.
- The archive prose binds each superseded record, which lets a partial
  re-run rebuild the identical provenance after the deletes.
