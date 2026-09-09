# GitHub Configuration

This directory contains GitHub-specific configuration files for Hephaestus.

## Workflows

### Lint Job (`workflows/_required.yml`)

Runs the full pre-commit hook suite (ruff, mypy, security checks) as the
required `lint` job on pull requests. Pre-commit runs the shared fast pytest
selection. Nightly CI owns full unit coverage and the remaining functional tests.

### Security Workflow (`workflows/security.yml`)

Scheduled and on-demand pip-audit scan for dependency vulnerabilities.

### Release Workflow (`workflows/release.yml`)

Builds and publishes the package to PyPI on version tag push (`v*`).

### Required Checks Workflow (`workflows/_required.yml`)

The consolidated required-status-check gate that runs on every pull request to
`main` (and on push to `main`). It aggregates lint, including the fast
pre-commit test selection, `uv-lock-check`, shellcheck, and the `pr-policy`
gate. The policy gate enforces `Closes #N`, DCO trailers, the Conventional
Commit PR title used for squash history, and every branch commit subject.
It also aggregates security scans, workflow-schema validation, and version
sync. `nightly-tests.yml` owns full coverage, remaining functional tests,
package and installed-CLI checks, shell tests, and Pi conformance. Cryptographic
commit signatures are enforced by the active `homeric-main-baseline` ruleset. The
automation loop runs `$athena:pr-review` and owns the
`state:implementation-go` label. `merge_wait` uses exact-head queue admission
when the effective ruleset requires a merge queue. Direct merge requires strict
update protection that the actor cannot bypass. Each request requires fresh
reviewed-head, open-`main`, unarmed, exclusive-label, and required-check evidence.
The loop does not invoke `gh pr merge`, arm native auto-merge, or use an
administrator bypass. The privileged label-event auto-merge workflow remains removed.

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
