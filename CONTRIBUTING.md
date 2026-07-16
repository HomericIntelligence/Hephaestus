# Contributing to Hephaestus

Thank you for considering contributing to Hephaestus! We welcome contributions from the community.

## Code of Conduct

This project follows the [HomericIntelligence Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## Your first day

New here? This is the shortest path from a fresh clone to a merged PR. Each step
links to the full section below.

1. **Set up the environment** ([Development Setup](#development-setup)) — install
   Pixi, then `just bootstrap` (one command: deps + editable install + pre-commit
   hooks).
2. **Confirm the toolchain works** — run `just check` (lint + format-check +
   typecheck) and `pixi run pytest tests/unit`. Green here means your machine is
   ready.
3. **Pick an issue** ([Code Contributions](#code-contributions)) — pick or open a
   GitHub issue, then branch as `<issue-number>-description`.
4. **Make the change test-first** ([Testing](#testing)) — write a failing test,
   make it pass, keep coverage at 83%+ (target 90%).
5. **Open the PR** ([Pull Request Process](#pull-request-process)) — sign every
   commit (`git commit -S`), use a Conventional Commit title, put
   `Closes #<issue-number>` on its own line in the body, and keep auto-merge
   disabled. After an unconditional independent strict-review GO, a maintainer
   performs the manual squash merge.

If anything in steps 1–2 fails, see [Platform Support](#platform-support) — the
pixi dev environment is `linux-64` only by design.

## Planning artifacts

Before opening a PR, locate the work in:

- [`docs/ROADMAP.md`](docs/ROADMAP.md) — current and planned releases.
- [`docs/DEFINITION_OF_DONE.md`](docs/DEFINITION_OF_DONE.md) — the
  completion bar every PR is reviewed against.
- [`docs/TECH_DEBT.md`](docs/TECH_DEBT.md) — debt-tracking convention
  and `wontfix` gate.
- [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) — bug and
  feature templates.
- [`.github/pull_request_template.md`](.github/pull_request_template.md)
  — the PR scaffolding (`Closes #N` line is enforced by the
  `pr-policy` CI gate).

Hephaestus uses trunk-based development: create one short-lived feature
branch per issue, open a pull request, squash-merge it back to `main`, and cut
releases from signed `vX.Y.Z` tags; there are no release branches.

Keep PRs small: prefer one issue per PR so each change can be reviewed
and reverted independently. As a rough guide, aim to keep PRs under
~500 changed lines (XS <10 · S <50 · M <250 · L <500 · XL 500+); split
larger PRs unless the change is inherently atomic. Every PR is reviewed
against the [Definition of Done](docs/DEFINITION_OF_DONE.md). Releases
are cut on demand by pushing a signed `vX.Y.Z` git tag (see
[`docs/RELEASING.md`](docs/RELEASING.md)).

## How to Contribute

### Reporting Bugs

- Use the GitHub issue tracker
- Describe the bug clearly
- Include steps to reproduce
- Mention your environment (OS, Python version, etc.)

### Suggesting Enhancements

- Use the GitHub issue tracker
- Explain the enhancement in detail
- Provide use cases
- If possible, suggest implementation approaches

### Code Contributions

1. Open (or pick up) a GitHub issue describing the change.
2. Create a feature branch named `<issue-number>-description`.
3. Make your changes.
4. Write/update tests.
5. Update documentation.
6. Submit a pull request — see [Pull Request Process](#pull-request-process) below.

## Development Setup

1. Install Pixi: <https://pixi.sh/install/>
2. Clone your fork
3. Bootstrap the project (installs deps, the editable package, and pre-commit
   hooks in one step): `just bootstrap`

   `just bootstrap` wraps `pixi install`, `pixi run dev-install`, and
   `pixi run pre-commit install`. If you do not have [`just`](https://just.systems/)
   installed, run those three commands manually instead.
4. Activate development environment: `pixi shell -e dev`
5. Before pushing, run the fast quality gate: `just check`
   (lint + format-check + typecheck). Run `just --list` to see every recipe.

### Platform Support

The pixi developer environment and the published wheel intentionally cover
different platform sets. Contributors and downstream users should know which
they are using:

| Install path                        | Platforms supported                    | Python      |
| ----------------------------------- | -------------------------------------- | ----------- |
| `pixi install` (development)        | `linux-64` only                        | 3.10 (pinned by `pixi.toml`) |
| `pip install HomericIntelligence-Hephaestus` (wheel) | Linux, macOS, Windows (any OS) | 3.10+ (see `requires-python` in `pyproject.toml`) |

Why the asymmetry:

- **Pixi is Linux-64 only by design.** `pixi.toml` declares
  `platforms = ["linux-64"]` with an explicit `# Do not re-enable` comment
  next to the other entries. Full local reproduction of the development
  environment (lint, mypy, test, pre-commit) is only supported on Linux.
- **The wheel supports the broader matrix advertised in `pyproject.toml`.**
  `requires-python` in `pyproject.toml` describes what `pip install` will accept.
  No platform-restriction classifier is published, so the wheel is installable
  on macOS and Windows.
- **Windows wheels pull in `tzdata` automatically.** The
  `"tzdata; platform_system == 'Windows'"` marker in `[project.dependencies]`
  exists because `hephaestus.github.rate_limit` uses `zoneinfo.ZoneInfo`,
  which has no IANA database bundled on Windows. POSIX installs skip this
  dependency.

If you need to develop or run the test suite on macOS or Windows, install the
wheel into a plain virtualenv (`pip install -e '.[dev]'`) — pixi tooling is
not available there, and any subpackage with POSIX-only assumptions is out
of scope for this project's supported development environment (track those separately).

### The `build/` directory

`build/` is gitignored **automation scratch**, not packaging output. The
automation loop writes work reports, loop logs, and audit artifacts there
(see `hephaestus/automation/loop_runner.py`). Despite the name, no build or
distribution artifacts come from it — the sdist `only-include` allowlist in
`pyproject.toml` excludes it. Never `git add` anything under `build/`; the
`check-build-dir-untracked` pre-commit hook enforces this (issue #1214). To
clear local scratch, stop any running automation loop first, then
`git clean -fdX build/` (removes only ignored files).

## Code Style

We follow these style guidelines:

- Python code: Formatted and linted with [Ruff](https://docs.astral.sh/ruff/)
- Type hints: Required for all public functions (enforced by mypy strict mode)
- Line length: 100 characters
- Target Python: 3.10+

Run the development tools:

```bash
pixi run format  # Format code with ruff format
pixi run lint    # Lint with ruff check
```

## Testing

All contributions must include appropriate tests:

- Unit tests for new functionality
- Integration tests for complex features
- Maintain or improve code coverage

Run tests with:

```bash
pixi run test
```

### Test environment requirements

The unit-test suite executes a small number of real subprocesses and therefore
assumes a POSIX-like development environment. Specifically:

- **`echo`, `false`, `ls`** on `PATH` — used by `tests/unit/automation/test_git_utils.py::TestRun`
  to exercise the `run()` wrapper end-to-end (four cases).
- **`git`** on `PATH` — used by `tests/unit/automation/test_session_naming.py`
  (`TestShortGithash::test_real_repo` and
  `TestCurrentTrunkGithash::test_falls_back_to_short_githash`) to create a
  throwaway repo inside `tmp_path` with `git init -q` and `git commit
  --allow-empty --no-gpg-sign`. Git environment variables (`GIT_DIR`,
  `GIT_WORK_TREE`, etc.) are scrubbed and author/committer identity is forced
  via `_git_test_env()` so the tests do not depend on the contributor's
  `~/.gitconfig`.

These cases are tagged with the `requires_posix` pytest marker and are skipped
automatically on `sys.platform == "win32"`. They run under macOS, Linux, and
WSL with no extra setup beyond `pixi install`. Windows contributors using Git
Bash / MSYS2 will execute them; pure-Windows-Python runs will skip them.
Tracking: #742.

## Documentation

- Update docstrings for code changes
- Add sections to README.md for new features
- Keep documentation clear and concise

## Version Management

The project uses **hatch-vcs dynamic versioning** — the version is derived from
git tags, not stored in a file:

- **Single source of truth**: the latest `vX.Y.Z` git tag. `pyproject.toml` declares
  `dynamic = ["version"]` with `[tool.hatch.version]` `source = "vcs"`; there is no
  static `[project].version`.
- **`pixi.toml` has no version field** — this is intentional. A pre-commit hook
  (`check-version-single-source`) rejects a `version` field in either file.

### Releasing a new version

You do not edit a version field. A release is cut by creating a signed git tag —
see [`docs/RELEASING.md`](docs/RELEASING.md) for the full workflow. `hephaestus-bump-version`
computes the next semver string and prints the `git tag` commands to run.

## Dependency Updates

- **Dependabot** is configured for `pip` (pyproject.toml dev extras) and is the
  **sole** manager of `github-actions`. It opens PRs automatically for those.
- **Renovate** (`renovate.json`) is configured with the `:pixi` preset and watches
  **only** conda-forge / pixi dependencies in `pixi.toml` — the package ecosystem
  Dependabot cannot parse. Renovate's GitHub Actions manager is explicitly disabled
  (`"github-actions": { "enabled": false }`) so the two tools never double-manage
  actions and emit duplicate PRs. Renovate opens grouped PRs on a weekly cadence,
  matching Dependabot's schedule. To manually refresh the lock file outside of that
  cycle:

  ```bash
  pixi update           # updates pixi.lock; commit alongside any range changes
  ```

- A pre-commit hook (`check-dep-sync`) verifies that any committed `requirements*.txt`
  entries fall within the `pixi.toml` range; it does not initiate updates.

## Pull Request Process

The `main` branch is protected. CI's `pr-policy` gate blocks a PR that lacks a
valid issue reference, Conventional Commit title, signed commits, or DCO
sign-offs:

1. **Sign every commit**: `git commit -S`. Verify with `git log --show-signature -1`.
2. **Reference the issue**: the PR body must contain the literal line `Closes #<n>`
   (capital `C`, no colon, on its own line). `Fixes`, `Resolves`, `closes`, and
   `Closes:` are **not** accepted.
3. **Sign off every commit**: include a DCO `Signed-off-by` trailer, normally
   with `git commit -s -S`.
4. **Use a Conventional Commit PR title**:
   `type(scope)!: concise description`. The title becomes the `main` subject
   when the PR is squash-merged. Every authored branch commit follows the same
   form; see the Definition of Done for the narrow Git-generated exceptions and
   the pre-#2157 history cutover.

During #2054's bootstrap, auto-merge must remain disabled. The pipeline verifies
that state and the advisory `auto-merge-policy` reports any armed PR, but it is
not a required check. An unconditional independent strict-review GO is required
before a maintainer manually runs `gh pr merge --squash`.

Also: ensure tests pass locally (`pixi run test`), keep commits to logical units with
[conventional commit](https://www.conventionalcommits.org/) messages, and never bypass
pre-commit hooks with `--no-verify`.

## Developer Certificate of Origin (DCO)

By contributing, you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/):
you have the right to submit the work under this project's open-source license and you agree it may be
distributed under those terms. You record that legal grant by adding a `Signed-off-by` trailer to **every**
commit:

```bash
git commit -s -S -m "type(scope): description"
```

This is **distinct** from the cryptographic signature requirement above, and both are required:

- **`-s` (`Signed-off-by:` trailer)** — the *DCO*. A legal attestation that you have the right to
  contribute the change and license it inbound to the project. It proves *provenance of the grant*.
- **`-S` (GPG/SSH signature)** — *cryptographic authorship/integrity*. It proves *who* authored the
  commit and that its contents were not tampered with. The `pr-policy` CI gate enforces `-S`.

You can set them together so you never forget:

```bash
git config commit.gpgsign true   # always -S
# add the sign-off per commit with -s (or via a prepare-commit-msg hook)
```

Both are now mechanically enforced: the `pr-policy` CI gate (Check 4) fails any PR
whose commits lack a valid `Signed-off-by: Name <email>` trailer, and the local
`dco-signoff-msg` `commit-msg` pre-commit hook rejects an un-signed-off commit
before it is created. To re-sign existing commits run:

```bash
git rebase --exec 'git commit --amend --no-edit -s' origin/main
```

## Questions?

Feel free to ask questions in GitHub issues or discussions.
