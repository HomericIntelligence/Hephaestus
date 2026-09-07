# ADR-0041: Versioned recovery-comment identity

- Status: Accepted
- Date: 2026-09-06
- Supersedes: ADR-0031 in part
- Tracks: #2960

## Context

Requirements recovery keeps its evidence in one actor-owned issue comment.
The comment marker contains a version and digest values. A marker-family
prefix identifies the recovery role, but it does not identify one full
marker line.

The shared comment API uses exact first-line markers for the plan and review
roles. Recovery must keep that rule and also accept correct recovery
comments from versions 1, 2, and 3. A full bounded comment journal is
necessary to find foreign, malformed, repeated, and duplicate claims before
the function writes a comment.

## Decision

`RECOVERY_PROVENANCE_PREFIX` identifies the namespace for recovery markers. A
recovery claim is one top-level Markdown line that starts with this prefix. A
claim is correct only when the recovery provenance checks accept the full
marker and its rendered body.

The recovery identity selector reads the full bounded issue-comment
journal. It returns one correct actor-owned recovery comment and its parsed
provenance. It stops with an error when a claim has leading whitespace or is
foreign or malformed. It also stops for a repeated claim or a duplicate
actor-owned comment. It returns no comment when the journal has no recovery
claim.

The two public recovery upsert paths use this selector before a mutation and
after each create or update. The selector lets a create occur only when no
recovery comment exists. An update changes the selected comment in place. The
GitHub database ID must stay unchanged, and the full journal must show the
exact specified body after the mutation. The upsert and timeline compaction
paths do not remove a recovery duplicate.

Correct version 1 and version 2 comments stay identity candidates. Contextual
recovery must use version 3. A version migration keeps
the same GitHub database ID. This decision does not change the identity
contract for plan and review comments that use exact markers.

Selector and post-write identity failures use the planning retry
budget. They do not start one more retry loop. A retry uses the durable
plan publication receipt and does not publish the same plan again.

All Hephaestus-owned English technical text for this decision follows
ASD-STE100 Simplified Technical English, Issue 9, dated January 15, 2025.
Before release, the author and reviewer record a controlled-dictionary check
in the pull-request evidence. The repository does not contain a copy of the
standard.

## Alternatives considered

- Treat the recovery prefix as an exact marker. We rejected this alternative
  because rendered recovery markers contain version and digest fields after
  the prefix.
- Select the last recovery comment. We rejected this alternative
  because a duplicate or foreign comment can make the actor-owned record
  not available and cause an incorrect mutation.
- Remove duplicate recovery comments during compaction. We rejected this
  alternative because manual recovery is necessary before a step that removes
  data when the identity is not clear.
- Add a recovery-specific retry loop. We rejected this alternative because
  these retries can use more than the stage budget and republish a plan.

## Consequences

- Recovery comments from versions 1, 2, and 3 can migrate in place.
- Foreign, malformed, repeated, and duplicate claims stop before mutation.
- Comment IDs give stable identity during recovery version updates.
- The planning stage keeps its bounded retry and receipt model.
- This decision does not change the plan and review exact-marker contract.
