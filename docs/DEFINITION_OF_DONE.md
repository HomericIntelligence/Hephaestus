# Definition of Done

Hephaestus's Definition of Done is the union of (a) what the PR template
enforces socially, and (b) what CI enforces mechanically. This document is the
single, discoverable place where both lists live. If you change the PR template
or a CI gate, also update the corresponding row here.

A piece of work is **done** when every requirement below is true.

Branch naming is optional guidance, not a completion gate. Prefer
`<issue-number>-<description>` for new branches. Other valid Git branch names
do not require an exception, a rename, or a replacement PR. Reviewers must not
block a PR because it uses another branch naming format.

## For every PR

| # | Requirement | Enforced by |
|---|-------------|-------------|
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
| 25 | For a manual contribution, verify the canonical `upstream`, fetch `upstream/main`, rebase only for a conflict, necessary dependency, or explicit request, verify commit signatures and one matching final DCO trailer, and record clean exact-head evidence from focused checks. Cover affected behavior and each new or changed test; use applicable documentation checks for documentation-only changes. ADR-0053 and ADR-0055 define this manual sequence. No complete local suite is required. The automation loop retains ADR-0048. Pre-commit and required PR checks run the shared fast selection. Nightly CI owns full validation. | Author and source reviewer; focused local checks, fast tests in `lint`, full suites in nightly CI |

### Focused test evidence

For a manual contribution, use this verification sequence:

1. Run focused tests during implementation. Agents must use test-only
   subagents for these checks.
2. Verify the canonical target remote and fetch its current `main` branch.
   Record the feature branch start commit as the local review base. A later
   target-branch advance does not change that recorded base.
3. Rebase only for an actual conflict, a necessary dependency, or an explicit
   request. If a rebase is necessary, use `git rebase -S <verified-target>`.
   Keep unrelated branches and worktrees unchanged.
4. Commit with `git commit -s -S`. Verify every contribution commit with
   `git verify-commit <commit>`. Require exactly one final `Signed-off-by`
   trailer that matches the commit author. A rebase must preserve these checks.
5. Require `git status --porcelain=v1 --untracked-files=all` to have no output.
   Record `git rev-parse HEAD`.
6. Run focused checks on this head. Select tests for the affected behavior and
   each new or changed test. Use locked pytest commands with explicit paths or
   node IDs and `--override-ini="addopts="`. Confirm nonempty collection and
   success. For documentation-only changes, use applicable existing
   documentation checks. Do not require a complete local suite.
7. Record `git rev-parse HEAD` again. Require the same value and an empty
   `git status --porcelain=v1 --untracked-files=all` result. Record each command,
   source revision, result, and test summary.
8. After a source change, repeat the affected checks and signature and DCO
   verification for the resulting head. Do not require another rebase solely
   because a check ran or the target branch advanced.

Historical results retain their original executed revision. Do not report
those results as a new run on the current head. Any use of historical evidence
requires an explicit compatibility basis for the relevant inputs. Required CI
must still pass for the merge candidate under the current merge policy.

A hook result counts as focused evidence only when it collected and passed the
selected tests on the applicable head and recorded the command and result.
No complete local suite is required before PR creation or merge. Required PR
checks supply exact-head merge evidence; nightly CI supplies full-suite and
coverage results. A fast PR result is not full-suite evidence. See ADR-0053.

Source review does not execute tests and does not require a local review image.
The reviewer inspects source and recorded evidence. CI/CD results remain
separate from source-review authority and protected merge admission.

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
