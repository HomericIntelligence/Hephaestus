# Workflow Context for Hephaestus

This file describes the typical development workflow for Hephaestus.

## Development Cycle

1. **Issue Creation**: Create GitHub issue describing the utility or enhancement
2. **Branch Creation**: Create feature branch named `{issue-number}-description`
3. **Implementation**: Write utility functions with comprehensive tests
4. **Quality Checks**: Run linters, type checker, and tests
5. **Documentation**: Update or create relevant documentation
6. **Pull Request**: Create PR linking to original issue

## Code Review Process

1. **Automated Checks**: Pull-request checks are an independent GitHub concern; automation-loop stages neither read, modify, nor wait on their status.
2. **PR Policy Gate**: The `pr-policy` CI gate enforces the `Closes #<issue-number>` body line and cryptographically signed commits (`git commit -S`). See `CLAUDE.md` §"Working with GitHub" for the canonical policy.
3. **Loop-owned Review**: `pr_review` invokes `$athena:pr-review --ci-free`; a clean GO applies `state:implementation-go` without consulting CI/CD.
4. **Merge**: Do not manually enable auto-merge. `merge_wait` conditionally arms only when the loop-owned `state:implementation-go` label remains present; it does not consult CI/CD or an external review artifact.

## Testing Workflow

1. **Unit Tests**: Test individual utility functions in isolation
2. **Integration Tests**: Test utility functions together
3. **Edge Case Tests**: Test boundary conditions and error cases
4. **Cross-Platform Tests**: Ensure compatibility across platforms

## Release Process

1. **Version Bump**: Update version number according to semver
2. **Tag Release**: Create Git tag for the release
3. **Release Notes**: Generate via `gh release create --generate-notes`
4. **Publish**: Publish to package repository if applicable
