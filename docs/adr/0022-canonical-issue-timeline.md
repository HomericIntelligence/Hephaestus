# ADR-0022: Canonical two-comment issue timeline

- Status: Superseded by [ADR-0031](0031-bounded-recovery-artifact-roles.md)
- Date: 2026-08-08
- Tracks: #2719

## Context

Appending every plan revision, review, skip explanation, recovery payload, and
raw plan diff made long-running issues too large for useful agent context and
human review. The history duplicated source-control evidence while stale
reviews competed with the current decision. Labels already provide durable
pipeline state and pull requests already own implementation discussions.

## Decision

Automation retains exactly two actor-owned comments on a linked issue: the
latest canonical implementation plan and the latest canonical plan review.
Each revision replaces those comments in place. The current plan may contain a
bounded hidden set of prior plan fingerprints for oscillation detection and a
concise cumulative `Changes from Review` section containing high-level bullets.
Raw patches are rejected before publication.

Skip reasons are label-plus-log facts. Transient implementation-reply recovery
records are stored on the pull request, and implementation responses remain in
native PR review threads. A dry-run-first migration command removes only
strictly recognized comments GitHub proves were authored by the authenticated
actor; issue bodies and human or foreign comments are never mutated.

## Alternatives considered

- Retain append-only history but truncate prompt context: rejected because the
  GitHub issue itself remains noisy and unbounded.
- Store complete old plans in hidden comments: rejected because hidden markup
  still consumes API and model context and duplicates Git history.
- Delete every marker-bearing comment: rejected because marker text alone is
  not ownership proof and could destroy human or foreign content.

## Consequences

The linked issue remains compact and stable across arbitrarily many review
rounds. Detailed implementation evidence lives in commits and PR-native review
threads. Public comment history is no longer the crash-recovery mechanism;
canonical pointer readback and bounded fingerprints provide restart safety.
Legacy history remains readable until the compaction migration has converged.

This decision predated autonomous requirements recovery. ADR-0031 preserves
the two ordinary planning roles while defining the bounded recovery roles that
must coexist with them.
