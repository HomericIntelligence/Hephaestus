# GitHub Configuration

This directory contains GitHub-specific configuration files for ProjectHephaestus.

## Workflows

### Test Workflow (`workflows/test.yml`)

Continuous Integration pipeline that runs on every push and pull request to `main`.

**Matrix:**

- OS: `ubuntu-latest`
- Python: `3.12`
- Test types: `unit`, `integration`

**Jobs:**

- **Unit tests**: pytest with coverage (≥80%)
- **Integration tests**: import smoke tests + wheel build/install
- **Structure check**: enforces test mirrors source layout

**Status Badge:**

```markdown
![Test](https://github.com/mvillmow/ProjectHephaestus/actions/workflows/test.yml/badge.svg)
```

### Pre-commit Workflow (`workflows/pre-commit.yml`)

Runs all pre-commit hooks (ruff, mypy, security checks) on pull requests.

### Security Workflow (`workflows/security.yml`)

Scheduled and on-demand pip-audit scan for dependency vulnerabilities.

### Release Workflow (`workflows/release.yml`)

Builds and publishes the package to PyPI on version tag push (`v*`).

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
