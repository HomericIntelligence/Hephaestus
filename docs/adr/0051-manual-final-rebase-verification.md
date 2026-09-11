# ADR-0051: Manual final-rebase verification

- Status: Accepted
- Date: 2026-09-11
- Tracks: #3111

## Context

ADR-0047 requires focused test evidence before pull request creation. It does
not make a complete local pytest run a prerequisite. ADR-0049 keeps that
focused-test rule and assigns the complete validation contract to nightly
continuous integration and continuous delivery (CI/CD).

A manual contribution can change during its final rebase. Test evidence from
the prior commit does not prove the new commit. A fork also does not make
`origin/main` the canonical project branch. In addition, a command that clears
the default pytest marker can start opt-in artifact, contract, performance, or
host tests that the contributor environment does not supply.

## Decision

For a manual contribution, require a complete normal local pytest run after
the final rebase and before pull request creation. If the branch changes after
pull request creation, repeat the final-rebase verification before merge. This
decision supersedes the focused-only prerequisite in ADR-0047 and ADR-0049 for
this manual final-rebase step. It does not change the fast pull-request checks
or the nightly CI/CD contract in ADR-0049.

Configure `upstream` with the canonical
`https://github.com/HomericIntelligence/Hephaestus.git` URL. Verify this URL,
fetch `upstream/main`, and stop if the fetch fails. Rebase with
`git rebase -S upstream/main`. Verify the signature of each rebased commit
before the test run.

Record `git rev-parse HEAD` before and after the test run. Require
`git status --porcelain=v1 --untracked-files=all` to have no output before and
after the run. The two head values must be equal.

Use this locked normal-test command:

```bash
uv run --locked pytest tests --override-ini="addopts=" -v --strict-markers \
  -m "not performance and not contract and not artifact and not codex_release_artifact and not pyxis"
```

The excluded profiles keep their explicit CI/CD or host controls. A pre-push
hook can supply this local result only when it runs this exact command on the
same clean head and records the command, head, status, and test summary. If the
branch changes, repeat the final-rebase verification.

The automation loop continues to use ADR-0048. Its host-owned validation and
exact-head CI/CD gates do not use this manual contributor sequence.

## Alternatives considered

Focused tests alone do not meet issue #3111 because they do not validate the
final rebased branch. A command that selects all marker profiles is not valid
in the documented contributor environment. A new wrapper would add a second
owner for a command that the documentation can state directly.

## Consequences

Manual contributions take more time before merge. The evidence identifies one
clean commit and one runnable test selection. Nightly CI/CD remains the
authoritative complete validation contract and continues to run the excluded
profiles with their required fixtures and host controls.
