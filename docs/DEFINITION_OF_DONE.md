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
| 3 | Every commit on the branch is cryptographically signed and DCO-signed (`git commit -S -s`) | CI gate `pr-policy` |
| 4 | Auto-merge remains disabled during #2054's fail-closed bootstrap. An unconditional independent strict-review GO is required before a manual squash merge; #2055 restores queue-owned arming. | Advisory `auto-merge-policy` and human review |
| 5 | Commit messages follow Conventional Commits (`type(scope): description`) | CI gate `pr-policy` (Check 3) + local `commit-msg` hook `conventional-commit-msg` |
| 6 | `pixi run ruff check hephaestus/ tests/` passes | CI job `lint` |
| 7 | `pixi run ruff format --check hephaestus/ tests/` passes (no files would be reformatted) | CI job `lint` |
| 8 | `pixi run mypy` returns `Success: no issues found in N source files` | CI job `lint` |
| 9 | Full unit suite passes: `pixi run pytest tests/unit` (currently 2,500+ tests across 4 Python versions) | CI jobs `unit-tests`, `test (ubuntu-latest, 3.10/3.11/3.12/3.13, unit)` |
| 10 | Coverage gate satisfied: `--cov-fail-under=83` (configured in `pyproject.toml [tool.coverage.report].fail_under`) | CI job `unit-tests` |
| 11 | No new warnings introduced (pytest, deprecation, ruff) | PR reviewer |
| 12 | Integration tests pass: `pixi run test tests/integration` (installs the editable package first) | CI job `integration-tests` |
| 13 | Shell tests pass: `pixi run test-shell` | CI job `shell-tests` |
| 14 | Schema validation passes (CLI inventory, YAML/Markdown structure) | CI job `schema-validation` |
| 15 | Dep sync check passes (pyproject.toml then pixi.toml then pixi.lock) | CI job `deps/version-sync` |
| 16 | Secrets scan finds no leaks | CI jobs `security/secrets-scan`, gitleaks in `_required.yml` |
| 17 | Dependency vulnerability scan passes | CI jobs `security/dependency-scan`, `pip-audit` in `_required.yml` |
| 18 | Markdownlint passes on all `.md` changes | CI job `lint` (pre-commit hook) |
| 19 | Shellcheck passes on all shell scripts | CI job `shellcheck` |
| 20 | Yamllint passes on all YAML changes | CI job `lint` |
| 21 | Pre-commit hooks pass on the diff | CI job `lint` (pre-commit suite folded into `lint` per #1173) |
| 22 | Every review thread is resolved (including bot-authored threads) | Org ruleset `required_review_thread_resolution` |

> **Which of these actually block the merge button?** Both the classic branch
> protection contexts (`required-checks-gate` and the two Python 3.12 matrix
> contexts) and the direct GitHub ruleset contexts documented in
> [`docs/ci/required-checks.md`](ci/required-checks.md) do. The aggregate gate
> excludes advisory `auto-merge-policy` during #2054 so it cannot block the
> independently reviewed manual bootstrap merge.

## For new features

In addition to the universal checklist:

| # | Requirement | Enforced by |
|---|-------------|-------------|
| F1 | Public functions have Google-style docstrings | Convention (PR reviewer) |
| F2 | New `main()` entry points have at least smoke tests (one happy-path, one error-path) | Coverage gate (rejects untested code if it drops total under 83%) |
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
change the PR template's checklist, reconcile rows 1-5 here.

If you find yourself describing a "DoD requirement" in a comment, code review, or
Slack message that isn't already in this document, add it here.
