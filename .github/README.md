# GitHub Configuration

This directory contains GitHub-specific configuration files for ProjectHephaestus.

## Workflows

### CI Workflow (`workflows/ci.yml`)

Continuous Integration pipeline that runs on every push and pull request to `main`.

**Jobs:**
- **Test**: Runs pytest on Python 3.8-3.12
- **Lint**: Code quality checks (flake8, black, mypy)
- **Coverage**: Test coverage reporting

**Status Badge:**
```markdown
![CI](https://github.com/HomericIntelligence/ProjectHephaestus/workflows/CI/badge.svg)
```

## Future Additions

Potential future GitHub configurations:

- **Issue Templates**: `.github/ISSUE_TEMPLATE/`
- **Pull Request Template**: `.github/PULL_REQUEST_TEMPLATE.md`
- **Dependabot**: `.github/dependabot.yml`
- **Code Owners**: `.github/CODEOWNERS`
- **Release Workflow**: `.github/workflows/release.yml`
- **Documentation Deploy**: `.github/workflows/docs.yml`

## Maintenance

To update the CI workflow:

1. Edit `.github/workflows/ci.yml`
2. Test locally if possible
3. Commit and push to trigger workflow
4. Monitor Actions tab on GitHub
5. Fix any issues and iterate

## Security

The CI workflow follows GitHub Actions security best practices:
- No untrusted input in `run:` commands
- Environment variables used for user-controlled data
- Dependencies pinned with version constraints
- Actions pinned to specific versions
