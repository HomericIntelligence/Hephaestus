# Definition of Done

Hephaestus's Definition of Done is the union of (a) what the PR template
enforces socially, and (b) what CI enforces mechanically. This document is the
single, discoverable place where both lists live. If you change the PR template
or a CI gate, also update the corresponding row here.

A piece of work is **done** when every item below is true.

## For every PR

| # | Requirement | Enforced by |
|---|-------------|-------------|
| 1 | Branch named `<issue-number>-<description>` | Convention (PR reviewer) |
| 2 | PR body contains the literal line `Closes #<issue-number>` (capital C, no colon, on its own line) | CI gate `pr-policy` (`.github/workflows/_required.yml`) |
| 3 | Every commit is cryptographically signed and DCO-signed (`git commit -S -s`) | `homeric-main-baseline` ruleset (`required_signatures`) + CI `pr-policy` DCO check |
| 4 | `pr_review` writes loop-owned `state:implementation-go` only after the typed reviewer verdict is `GO`. Fresh GitHub facts must also confirm the exact open, unarmed reviewed head, complete thread state, and exclusive-label readback. A missing, malformed, `NOGO`, or `BLOCKED` verdict fails closed. A grade is audit metadata only. Immediately before each server merge request, `merge_wait` requires that label, the current-process reviewed-head proof, no unresolved review threads, and complete passing required status evidence for the exact head. Completed Check Runs can have a `success`, `neutral`, or `skipped` conclusion. Commit statuses can satisfy only unbound contexts and must have the `success` state. Optional Check Runs do not grant or revoke merge eligibility. A second GitHub user and a marked `APPROVED` review are not required. Required status evidence is a separate merge gate and does not authorize review or replace the reviewed-head proof. The merge budget (default: five) bounds actual requests and safe pre-dispatch retries, not readiness polling. The effective policy selects the server route. A required merge queue uses exact-head queue admission. Direct merge requires effective strict-update protection and an actor that cannot bypass it. No queue stage uses `gh pr merge` or mutates native auto-merge. | Queue gate and exact-head CI gate |
| 5 | The PR title uses an authored Conventional Commit form because it becomes the squash subject; each branch commit uses that form or a recognized Git-generated machinery form | CI gate `pr-policy` (Check 2) + local `commit-msg` hook `conventional-commit-msg` |
| 6 | `uv run ruff check hephaestus/ tests/` passes | CI job `lint` |
| 7 | `uv run ruff format --check hephaestus/ tests/` passes (no files would be reformatted) | CI job `lint` |
| 8 | `uv run mypy hephaestus/ scripts/ tests/` returns `Success: no issues found in N source files` | CI job `lint` |
| 9 | Fast test suite passes: `just test` | CI job `lint` through the pre-commit hook |
| 10 | Coverage gate satisfied: `--cov-fail-under=85` (configured in `pyproject.toml [tool.coverage.report].fail_under`) | Nightly CI job `unit-coverage` |
| 11 | No new warnings introduced (pytest, deprecation, ruff) | PR reviewer |
| 12 | Full integration tests pass | Nightly CI job `functional-tests` |
| 13 | Shell tests pass: `just test-shell` | Nightly CI job `shell-tests` |
| 14 | Schema validation passes (CLI inventory, YAML/Markdown structure) | CI job `schema-validation` |
| 15 | The lockfile is current: `uv lock --check` | CI job `uv-lock-check` |
| 16 | Secrets scan finds no leaks | CI jobs `security/secrets-scan`, gitleaks in `_required.yml` |
| 17 | Dependency vulnerability scan passes | CI jobs `security/dependency-scan`, `pip-audit` in `_required.yml` |
| 18 | Markdownlint passes on all `.md` changes | CI job `lint` (pre-commit hook) |
| 19 | Shellcheck passes on all shell scripts | CI job `shellcheck` |
| 20 | Yamllint passes on all YAML changes | CI job `lint` |
| 21 | Pre-commit hooks pass on the diff | CI job `lint` (pre-commit suite folded into `lint` per #1173) |
| 22 | Every review thread is resolved (including bot-authored threads) | Org ruleset `required_review_thread_resolution` |
| 23 | New or revised English technical prose follows the [ASD-STE100 writing standard](asd-ste100.md); principle declarations and specialized principle statements do not change only to satisfy the standard | Author and PR reviewer |
| 24 | Each `required-checks-gate` dependency succeeds on pull-request and merge-group events; only `pr-policy` can skip on a push event | CI gate `required-checks-gate` + structural unit guard |
| 25 | For a manual contribution, verify the canonical `upstream`, fetch `upstream/main`, use a signed rebase, verify the new commit signatures, and create clean exact-head evidence for the complete normal local pytest selection. ADR-0051 defines this manual sequence. The automation loop uses the separate ADR-0048 rebase policy. Pre-commit and required PR checks run the shared fast selection. Nightly CI runs the complete validation contract. | Author and PR reviewer; fast tests in `lint`, full suites in nightly CI |

### Final-rebase test evidence

Complete normal-test evidence is valid only for the final rebased head. First,
verify that `upstream` has the canonical
`https://github.com/HomericIntelligence/Hephaestus.git` URL. Fetch
`upstream/main` with these commands:

```bash
test "$(git remote get-url upstream)" = "https://github.com/HomericIntelligence/Hephaestus.git"
git fetch --no-tags upstream refs/heads/main:refs/remotes/upstream/main
```

If a command fails, stop. Run the signed rebase. Then verify each new commit
signature:

```bash
git rebase -S upstream/main
git rev-list --reverse upstream/main..HEAD | while IFS= read -r commit; do
    git verify-commit "$commit" || exit 1
done
```

Require `git status --porcelain=v1 --untracked-files=all` to have no output.
Record `git rev-parse HEAD`. Run this command:

```bash
uv run --locked pytest tests --override-ini="addopts=" -v --strict-markers \
  -m "not performance and not contract and not artifact and not codex_release_artifact and not pyxis"
```

Record `git rev-parse HEAD` again. Require the same value and an empty
`git status --porcelain=v1 --untracked-files=all` result. Conflict-resolution
edits, later commits, and other branch changes invalidate the prior evidence.
After such a change, repeat the final-rebase sequence and test run. Record the
command, branch head, result, and test summary.

A pre-push hook result counts only when the hook runs that exact locked command
on the applicable head and records the result. A hook that omits the command
does not supply complete normal-test evidence. See ADR-0051.

### Conventional Commit history boundary

Authored subjects use `type(scope)!: description`, where scope and `!` are
optional and type is one of `build`, `chore`, `ci`, `docs`, `feat`, `fix`,
`perf`, `refactor`, `revert`, `style`, or `test`. PR titles must use this form
without a Git-machinery exception because the title becomes the squash-merge
subject on `main`.

The local hook and branch-commit portion of Check 2 also accept Git-generated
`"Merge "`, `"Revert "`, `fixup!`, and `squash!` subjects. Those exceptions do not
apply to PR titles.

Commits already present on `main` before the PR that closes issue #2157 are
grandfathered and must not be rewritten. Rewriting published history would
replace commit identities and invalidate existing signatures, tags, and
downstream references. The PR closing #2157 establishes the cutover: its title
and every later squash-merge title must satisfy the authored form above.

> **Which of these actually block the merge button?** The
> `required-checks-gate` context and the direct GitHub ruleset contexts documented in
> [`docs/ci/required-checks.md`](ci/required-checks.md) do. Review output is
> audit evidence only;
> `state:implementation-go` is automated implementation eligibility, not the
> complete merge authority. `merge_wait` additionally requires its
> current-process reviewed-head proof, no unresolved review threads, and
> complete passing required status evidence for the exact head before
> conditional merge admission. Check Runs must be complete and have a
> `success`, `neutral`, or `skipped` conclusion. Commit statuses can satisfy
> only unbound contexts and must have the `success` state. Required status
> evidence does not create review authorization; it is a separate merge gate.

## For new features

In addition to the universal checklist:

| # | Requirement | Enforced by |
|---|-------------|-------------|
| F1 | Public functions have Google-style docstrings | Convention (PR reviewer) |
| F2 | New `main()` entry points have at least smoke tests (one happy-path, one error-path) | Coverage gate (rejects untested code if it drops total under 85%) |
| F3 | New CLI scripts use `add_json_arg(parser)` and emit `emit_json_status(...)` on exit | CI integration test `TestCLIJsonFlag` in `tests/integration/test_cli_entry_points.py` |
| F4 | New CLI scripts appear in `pyproject.toml [project.scripts]` AND in the CLI table of `README.md` | CI gate via `hephaestus.scripts_lib.check_cli_table_sync` |
| F5 | If the work touches deprecated APIs, update `COMPATIBILITY.md` | PR reviewer |

## For bug fixes

| # | Requirement | Enforced by |
|---|-------------|-------------|
| B1 | A regression test exists that fails before the fix and passes after | PR reviewer |
| B2 | The commit message names the originating issue (`Closes #N`) and briefly describes the root cause, not just the symptom | PR reviewer + `pr-policy` gate |

## For refactors

| # | Requirement | Enforced by |
|---|-------------|-------------|
| R1 | Pure move-and-delegate (or pure rename) — no behavior change | PR reviewer |
| R2 | If the refactor moves code, the pre-existing test suite still exercises the moved code through its original public surface (delegating shims / `__init__.py` re-exports / `# noqa: F401` markers as needed) | Unit suite green at the same coverage level |
| R3 | Smoke tests exist for any previously-uncovered `main()` whose internals are being refactored, committed BEFORE the extraction commits | PR reviewer (bisectable commit history) |

## For release-blocking work

Beyond the universal checklist, a release-blocker is done only when:

- The change is documented in `COMPATIBILITY.md` (if it changes a stability-tiered subpackage's behavior) and `docs/MIGRATION.md` (if it requires consumer changes).
- The change is mentioned in the PR body's `## Summary` such that the auto-generated release notes (`gh release create --generate-notes`) read coherently.

## How to update this document

When you add or remove a CI gate, edit the matching row in this file in the same PR.
When you adjust the coverage gate's threshold, update row 10's value here. When you
change the PR template's checklist, reconcile the corresponding universal rows here.
When you change a required job or supported event, update the aggregate graph,
runtime census, skip policy, tests, and CI documentation in the same PR.

If you find yourself describing a "DoD requirement" in a comment, code review, or
Slack message that isn't already in this document, add it here.
