# ADR-0031: Bounded recovery artifact roles on issue timelines

- Status: Accepted
- Date: 2026-08-20
- Supersedes: ADR-0022
- Tracks: #2795, #2659

## Context

ADR-0022 established a compact issue timeline with one canonical plan and one
canonical plan review. ADR-0030 subsequently introduced two durable,
actor-owned recovery artifacts: a digest-bound recovered-requirements comment
and an explanation for an independently confirmed obsolete disposition.
Calling those artifacts exceptions to the two-comment rule left their
ownership, replay, and compaction semantics implicit.

## Decision

The canonical plan and canonical plan-review comments remain the two ordinary
planning roles. Requirements recovery adds one separate, bounded provenance
role. An obsolete disposition adds one separate, bounded explanation role.
Each role has an opaque marker, is mutable only when GitHub proves the
authenticated actor authored it, and is upserted in place; duplicate owned
copies of the same role are removed. Foreign marker collisions are inert.

The recovered-requirements role contains versioned SHA-256 digests for the
source issue body, evidence binding, and rendered requirements. A restart may
use it only after authenticating ownership and verifying that its source digest
matches the current issue body; otherwise it starts fresh recovery. Timeline
compaction verifies the provenance marker and requirements digest before
retaining the latest copy, but cannot establish a live body match from comment
metadata alone, so it never promotes that artifact to recovery authority.
The canonical plan separately retains the matching source digest as hidden
epoch metadata. This binds restart routing without folding recovered
requirements into the plan: a matching plan is post-recovery work, while a
missing or mismatched marker identifies a stale pre-recovery plan.
Recovery provenance also binds the live title and repository revision; legacy
artifacts without that complete context are not restart authority. Plan
publication, successor binding, and pending-review label normalization form a
resumable transaction, so retries complete missing follow-up writes without
republishing identical plan prose.

A self-verifying Athena finalized-plan marker lives in the issue body rather
than occupying a comment role. It seals the completed plan/review epoch after
Athena may delete those intermediate comments. The body marker therefore
suppresses recovery only while its exact `F` binding verifies. On first
verification Hephaestus adds `athena:finalized-plan` as durable observation
metadata; this is not a plan state and does not authorize implementation by
itself. First observation also verifies that the authenticated automation actor
owns the latest body edit. The marker plus metadata survive comment compaction without increasing
the bounded comment-role count. Later marker removal or drift starts a new
planning/recovery epoch and clears the metadata before a replacement plan can
advance.

An obsolete explanation is permitted only after matching planner and reviewer
GO dispositions and accompanies `state:skip`. It is an audit explanation, not
routing authority: restart reads labels and independently re-evaluates any
future semantic candidate. The recovery and obsolete roles are not plan
revisions and are never folded into plan/review history.

Compaction preserves at most one actor-owned comment per active role: plan,
plan review, recovered requirements, and obsolete explanation. Thus normal
recovered work has at most three retained comments (plan, review, recovery),
and an obsolete skipped issue has its bounded explanation rather than an
unbounded event log. Historical legacy records remain migration-only and are
removed only after strict ownership and marker validation.

## Alternatives considered

- Fold recovered requirements into the canonical plan: rejected because a
  fresh plan can supersede its recovery evidence while a restart still needs to
  validate the source-body binding.
- Keep ADR-0022's literal two-comment limit and store recovery only in logs:
  rejected because process-local logs cannot provide authenticated restart
  evidence after a crash.
- Permit append-only recovery comments: rejected because repeated recovery
  attempts would recreate the unbounded timeline that ADR-0022 eliminated.

## Consequences

The recovery protocol has explicit, bounded durable evidence without claiming
that a provenance comment is a third plan role. Restarts fail closed on stale
or malformed recovery evidence; compaction cannot silently erase a malformed
actor-owned marker. The timeline remains bounded by roles rather than by the
number of planning or recovery attempts.
