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

1. **Automated Checks**: Normal `$athena:pr-review` assesses PR check evidence; automation-loop stages do not modify CI/CD state.
2. **PR Policy Gate**: The active ruleset enforces cryptographic signatures; `pr-policy` retains an independent signature check and enforces the `Closes #<issue-number>` body line, Conventional Commit subjects, and DCO trailers. See `AGENTS.md` §"Working with GitHub" for the canonical policy.
3. **Loop-owned Review**: `pr_review` invokes `$athena:pr-review` with its normal default behavior; a clean GO applies `state:implementation-go`.
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
