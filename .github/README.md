# GitHub Configuration

This directory contains GitHub-specific configuration files for ProjectHephaestus.

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
![Test](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/test.yml/badge.svg)
```

### Pre-commit Workflow (`workflows/pre-commit.yml`)

Runs all pre-commit hooks (ruff, mypy, security checks) on pull requests.

### Security Workflow (`workflows/security.yml`)

Scheduled and on-demand pip-audit scan for dependency vulnerabilities.

### Release Workflow (`workflows/release.yml`)

Builds and publishes the package to PyPI on version tag push (`v*`).

### Required Checks Workflow (`workflows/_required.yml`)

The consolidated required-status-check gate that runs on every pull request to
`main` (and on push to `main`). It aggregates lint, markdownlint, `pixi-check`,
shellcheck, the `pr-policy` gate (enforces `Closes #N` and signed commits),
unit/integration/shell tests, wheel build, security scans (pip-audit, Gitleaks,
bandit), workflow-schema validation, and version-sync. It also runs the
advisory `auto-merge-policy` job (the auto-merge ↔ `state:implementation-go`
state machine), which is intentionally **not** a required check so its
timing-sensitive verdict never blocks merge — it fails its own job only on a
mismatch as a visible signal. The workflow re-runs on `auto_merge_enabled` /
`auto_merge_disabled` and `labeled` / `unlabeled` events so the
`auto-merge-policy` job converges without timing races.

### Enable Auto-Merge on Implementation GO Workflow (`workflows/enable-auto-merge-on-implementation-go.yml`)

Privileged label-event workflow that runs when `state:implementation-go` is
added to an open pull request. It does not checkout PR code; it only reads PR
metadata, verifies the GO label is still present, and enables squash auto-merge
with `gh pr merge --auto --squash` when auto-merge is not already armed.

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
