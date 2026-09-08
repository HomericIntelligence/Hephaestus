# ADR-0046: Target-specific source-review bootstrap

- Status: Accepted
- Date: 2026-09-07
- Tracks: #3007, #2701, PR #3006
- Extends: ADR-0039

## Context

The registered host-verification plan requires the supported macOS boundary.
Linux reports an unsupported-boundary receipt before it executes candidate
code. PR #3006 adds a Linux Pyxis and Enroot boundary, but that candidate
cannot supply authority for its own review.

## Decision

Deliver #3007 in two steps. First, review and merge its implementation from a
clean, signed `main` checkout on the supported macOS host. Use the existing
immutable `sandbox-exec` boundary. Then install the merged revision on the
Linux review host. Only that revision can evaluate the PR #3006 exception.

The exception applies only to `HomericIntelligence/Hephaestus`, issue #2701,
PR #3006, and boundary `linux-pyxis-enroot`. The drive-green CLI accepts
`--host-verification-bootstrap-comment ID` only with `--prs 3006`. It rejects
issue scope, discovery, multiple PR values, and other repositories. The
positive comment ID is a selector. It is not authority.

An authenticated operator supplies a PR comment with the exact first line
`<!-- hephaestus-host-verification-bootstrap:v1 -->`. One raw JSON object
follows that line. A Markdown fence is not accepted. Its only fields are
`repository`, `issue`, `pr`, `head_sha`, `base_sha`, `boundary`, `state`, and
`manifest`. Each manifest record has only `status` and `path`. The comment
must belong to the current authenticated actor and have an `OWNER`, `MEMBER`,
or `COLLABORATOR` association. The pipeline reads the comment; it cannot
create or approve a grant.

The grant must bind the exact head, checkout-derived branch point, and full
30-record operation map in
[`host_verification_bootstrap.py`](../../hephaestus/automation/host_verification_bootstrap.py).
Every map row is mandatory. The Git worker derives the manifest with rename
detection disabled from the same head and branch point as the review diff.
The grant, fresh manifest, and allowed map must agree exactly. Extra, missing,
duplicate, noncanonical, or different records are rejected. A rename appears
as deletion and addition; deletions and operations outside the map fail.

The observed PR #3006 head `c81cc334232abebd6621915627a3fc5ab5734361` has
32 records and is ineligible. Its owner must remove the changes to
`hephaestus/automation/host_coverage.py` and
`tests/unit/automation/test_host_coverage.py`. These changes belong to #3035.
Use a base that contains that repair, retain its base behavior, and obtain a
fresh review and separate authenticated grant for the resulting exact head.
This decision does not modify PR #3006 or supply that grant.

Only an authentic Linux unsupported-boundary result for the first fixed
command and exact reviewed head can enter this exception. The receipt retains
`ok=false`, `status=skipped`, `immutable_source=false`, `failure_kind=runner`,
`platform=linux`, and `error=unsupported_host_verification_boundary`. It
remains failed host evidence. The separate fenced reviewer block states that
source review alone is permitted and that skipped commands did not run.

An immutable process-local proof binds the authenticated comment ID and body
digest to the repository, issue, PR, head, branch point, and manifest digest.
The pipeline reads and validates the grant again before source-review
submission, before the GO-label write, and before each merge request. Round
cleanup discards the proof. Serialized data cannot restore it after a restart.
A restarted run must obtain a new proof and perform fresh review.

Omitting the selector disables the exception. The operator can revoke it by
setting the comment state to `revoked` or deleting the comment. A missing,
changed, foreign, duplicate, malformed, or revoked grant stops review. After
a bootstrap GO write, revocation requires the existing exact-head open and
unarmed guard, a NO-GO label, and readback that GO is absent before returning
without a merge request. Closure or merge makes the grant inert. Revoke it
after PR #3006 merges.

## Consequences

The exception supplies source-review authority only. It does not turn a skip
into a successful test, change structural review, or replace exclusive labels,
complete threads, exact-head required checks, and protected server merge
policy. CI results cannot create source-review authority.

The threat model includes candidate-controlled verification output, stale
head or base values, foreign comments, changed scope, replayed proof data,
and revoked grants. These inputs fail closed. The supported macOS review
path remains available without this exception.

## Alternatives considered

Rejected alternatives include an unsandboxed fallback, candidate-provided
validation authority, a selector-only grant, CI-only approval, and a generic
exception for other PRs. None preserves the required trust boundaries.

See the [operator runbook](../runbooks/ci-driver-stall.md#linux-review-bootstrap-for-pr-3006).
