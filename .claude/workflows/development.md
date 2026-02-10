# Workflow Context for ProjectHephaestus

This file describes the typical development workflow for ProjectHephaestus.

## Development Cycle

1. **Issue Creation**: Create GitHub issue describing the utility or enhancement
2. **Branch Creation**: Create feature branch named `{issue-number}-description`
3. **Implementation**: Write utility functions with comprehensive tests
4. **Quality Checks**: Run linters, type checker, and tests
5. **Documentation**: Update or create relevant documentation
6. **Pull Request**: Create PR linking to original issue

## Code Review Process

1. **Automated Checks**: All PRs must pass pre-commit hooks
2. **Peer Review**: At least one other developer review required
3. **Security Review**: Security-sensitive changes require special attention
4. **Merge**: Use rebase merge strategy with auto-merge enabled

## Testing Workflow

1. **Unit Tests**: Test individual utility functions in isolation
2. **Integration Tests**: Test utility functions together
3. **Edge Case Tests**: Test boundary conditions and error cases
4. **Cross-Platform Tests**: Ensure compatibility across platforms

## Release Process

1. **Version Bump**: Update version number according to semver
2. **Changelog**: Document changes in CHANGELOG.md
3. **Tag Release**: Create Git tag for the release
4. **Publish**: Publish to package repository if applicable
