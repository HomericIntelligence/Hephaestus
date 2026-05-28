# Contributing to ProjectHephaestus

Thank you for considering contributing to ProjectHephaestus! We welcome contributions from the community.

## Code of Conduct

This project follows the [HomericIntelligence Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

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
3. Install dependencies: `pixi install`
4. Install the package itself (editable) so `import hephaestus` works in the
   environment: `pixi run dev-install`
5. Activate development environment: `pixi shell -e dev`

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

- **Dependabot** is configured for `pip` (pyproject.toml dev extras) and
  `github-actions`. It opens PRs automatically for those.
- **Renovate** (`renovate.json`) is configured with the `:pixi` preset and watches
  conda-forge / pixi dependencies in `pixi.toml` — the package ecosystem Dependabot
  cannot parse. Renovate opens grouped PRs on a weekly cadence, matching Dependabot's
  schedule. To manually refresh the lock file outside of that cycle:

  ```bash
  pixi update           # updates pixi.lock; commit alongside any range changes
  ```

- A pre-commit hook (`check-dep-sync`) verifies that any committed `requirements*.txt`
  entries fall within the `pixi.toml` range; it does not initiate updates.

## Pull Request Process

The `main` branch is protected. CI's `pr-policy` gate enforces three rules — a PR
that violates any of them is blocked:

1. **Sign every commit**: `git commit -S`. Verify with `git log --show-signature -1`.
2. **Reference the issue**: the PR body must contain the literal line `Closes #<n>`
   (capital `C`, no colon, on its own line). `Fixes`, `Resolves`, `closes`, and
   `Closes:` are **not** accepted.
3. **Enable auto-merge**: `gh pr merge --auto`.

Also: ensure tests pass locally (`pixi run test`), keep commits to logical units with
[conventional commit](https://www.conventionalcommits.org/) messages, and never bypass
pre-commit hooks with `--no-verify`.

## Questions?

Feel free to ask questions in GitHub issues or discussions.
