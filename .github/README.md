# GitHub Configuration

This directory contains GitHub-specific configuration files for Hephaestus.

## Workflows

### Test Workflow (`workflows/test.yml`)

Continuous Integration pipeline that runs on every push and pull request to `main`.

**Matrix:**

- OS: `ubuntu-latest`
- Python: `3.10` / `3.11` / `3.12` / `3.13`
- Test types: `unit`, `integration`

**Jobs:**

- **Unit tests**: pytest with coverage (≥80%)
- **Integration tests**: import smoke tests + wheel build/install
- **Structure check**: enforces test mirrors source layout

**Status Badge:**

```markdown
![Test](https://github.com/HomericIntelligence/Hephaestus/actions/workflows/test.yml/badge.svg)
```

### Pre-commit Workflow (`workflows/pre-commit.yml`)

Runs all pre-commit hooks (ruff, mypy, security checks) on pull requests.

### Security Workflow (`workflows/security.yml`)

Scheduled and on-demand pip-audit scan for dependency vulnerabilities.

### Release Workflow (`workflows/release.yml`)

Builds and publishes the package to PyPI on version tag push (`v*`).

### Required Checks Workflow (`workflows/_required.yml`)

The consolidated required-status-check gate that runs on every pull request to
`main` (and on push to `main`). It aggregates lint, `pixi-check`,
shellcheck, the `pr-policy` gate (enforces `Closes #N` and signed commits),
unit/integration/shell tests, wheel build, security scans (pip-audit, Gitleaks,
bandit), workflow-schema validation, and version-sync. It also runs the
advisory `auto-merge-policy` job. During #2054's fail-closed bootstrap it
signals any open PR with auto-merge armed; it is intentionally **not** a
required check so it does not block the manually reviewed bootstrap merge.
The privileged label-event auto-merge workflow was removed. #2055 restores
queue-owned arming only after a head-bound independent strict-review proof.

### Auto-Tag Workflow (`workflows/auto-tag.yml`)

Manually dispatched (`workflow_dispatch`) release-tagging helper. Computes the
next `vX.Y.Z` tag by bumping the requested component (`patch` / `minor` /
`major`) from the highest existing tag, then pushes it — which in turn triggers
`release.yml`.

## Maintenance

To update a workflow:

1. Edit the relevant `.github/workflows/*.yml` file
2. Test locally if possible
3. Commit and push to trigger the workflow
4. Monitor the Actions tab on GitHub

## Security

Workflows follow GitHub Actions security best practices:

- No untrusted input in `run:` commands
- Environment variables used for user-controlled data
- Dependencies pinned with version constraints
- Actions pinned to specific SHAs (release.yml) or versions
