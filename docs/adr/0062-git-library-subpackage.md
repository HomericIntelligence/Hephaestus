# ADR-0062: hephaestus.git is the Git-family library subpackage

- Status: Proposed
- Date: 2026-11-09
- Tracks: #3504

## Context

Epic #3504 groups the Git-facing modules into one library subpackage. Today
`hephaestus/git/` holds `__init__.py` and `gh/` only (#3513, merged). The
planned moves put the git-lifecycle modules (#3512), the `hephaestus/github/`
tree (#3514), the pipeline GitHub tiers (#3515), and host verification
(#3517) under it. ADR-0001 now names `git` in the library-layer list, so the
subpackage needs a recorded home decision.

## Decision

1. `hephaestus/git/` is a library subpackage. It holds `gh/`, `github/`,
   `github/api/`, `github/policy/`, and `verification/`.
2. `github_api/` moves under `hephaestus/git/`.
3. The `[automation]` extra shrinks as the moved modules leave the product
   layer.
4. The boundary check for the subpackage is AST-based. Issue #3505 owns it.
5. The ADR-0001 library-layer rules apply to every module under
   `hephaestus/git/`.

## Alternatives considered

- Keep `hephaestus/github/` as a top-level library peer. Rejected: the
  Git-family modules then stay split across two roots, and the library-layer
  list does not name one home for them.
- Extract a separate distribution. Rejected: it repeats the ADR-0001
  carve-out cost (a new repository, a second lockfile, and a CI sweep) with
  no current requirement.

## Consequences

- `hephaestus/github/` disappears in #3514 without a compatibility shim.
  Documents that name it follow in the gated documentation follow-up for
  #3525.
- Each move issue carries its own import, test, and document edits.
- The `[automation]` extra declares fewer modules after #3514 and #3515.
