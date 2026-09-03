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
| 4 | `pr_review` writes loop-owned `state:implementation-go` only after the typed reviewer verdict is `GO`. Fresh GitHub facts must also confirm the exact open, unarmed reviewed head, complete thread state, and exclusive-label readback. A missing, malformed, `NOGO`, or `BLOCKED` verdict fails closed. A grade is audit metadata only. The label is automated implementation eligibility, not operator authorization. Immediately before each individual SHA-conditional ordinary REST squash-merge request, `merge_wait` requires that label, the current-process reviewed-head proof, and exactly one trusted, unedited marked `APPROVED` GitHub review bound to the exact head. CI readiness and review prose never authorize merging. The merge budget (default: five) bounds actual requests and transport-ambiguity retries, not readiness polling. No queue stage uses `gh pr merge`, mutates native auto-merge, manages a merge queue, or bypasses protection. | Queue gate and human review |
| 5 | The PR title uses an authored Conventional Commit form because it becomes the squash subject; each branch commit uses that form or a recognized Git-generated machinery form | CI gate `pr-policy` (Check 2) + local `commit-msg` hook `conventional-commit-msg` |
| 6 | `uv run ruff check hephaestus/ tests/` passes | CI job `lint` |
| 7 | `uv run ruff format --check hephaestus/ tests/` passes (no files would be reformatted) | CI job `lint` |
| 8 | `uv run mypy hephaestus/ scripts/ tests/` returns `Success: no issues found in N source files` | CI job `lint` |
| 9 | Full unit suite passes: `uv run pytest tests/unit` | CI jobs `unit-tests` and `test (ubuntu-latest, 3.13, unit)` |
| 10 | Coverage gate satisfied: `--cov-fail-under=80` (configured in `pyproject.toml [tool.coverage.report].fail_under`) | CI job `unit-tests` |
| 11 | No new warnings introduced (pytest, deprecation, ruff) | PR reviewer |
| 12 | Integration tests pass: `uv run pytest tests/integration` | CI job `integration-tests` |
| 13 | Shell tests pass: `just test-shell` | CI job `shell-tests` |
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

> **Which of these actually block the merge button?** Both the classic branch
> protection contexts (`required-checks-gate` and the two Python 3.13 matrix
> contexts) and the direct GitHub ruleset contexts documented in
> [`docs/ci/required-checks.md`](ci/required-checks.md) do. Review output is
> audit evidence only;
> `state:implementation-go` is automated implementation eligibility, not the
> complete merge authority. `merge_wait` additionally requires its
> current-process reviewed-head proof and exactly one trusted, unedited marked
> operator approval for the exact head before conditional merge admission.
> Reporting checks do not create operator authorization.

## For new features

In addition to the universal checklist:

| # | Requirement | Enforced by |
|---|-------------|-------------|
| F1 | Public functions have Google-style docstrings | Convention (PR reviewer) |
| F2 | New `main()` entry points have at least smoke tests (one happy-path, one error-path) | Coverage gate (rejects untested code if it drops total under 80%) |
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
