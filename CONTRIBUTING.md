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

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Write/update tests
5. Update documentation
6. Submit a pull request

## Development Setup

1. Install Pixi: <https://pixi.sh/install/>
2. Clone your fork
3. Install dependencies: `pixi install`
4. Activate development environment: `pixi shell -e dev`

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

The project version is managed as follows:

- **Single source of truth**: `pyproject.toml` under `[project].version`
- **Secondary files**: `VERSION` and `hephaestus/__init__.py` (`__version__`) are kept in sync
- **`pixi.toml` has no version field** — this is intentional. The `[workspace]` section in `pixi.toml`
  is for environment metadata only; the package version comes from `pyproject.toml` via the editable install

### Updating the version

1. Update `version` in `pyproject.toml` under `[project]`
2. Update secondary files using the version manager:

   ```python
   from hephaestus.version.manager import VersionManager
   VersionManager().update("X.Y.Z")
   ```

   This updates `VERSION` and any `__init__.py` files that contain `__version__`.

3. Do **not** add a `version` field to `pixi.toml` — a pre-commit hook (`check-version-single-source`)
   will reject it

## Pull Request Process

1. Ensure tests pass locally
2. Squash commits into logical units
3. Write a clear commit message
4. Reference any related issues
5. Request review from maintainers

## Questions?

Feel free to ask questions in GitHub issues or discussions.
