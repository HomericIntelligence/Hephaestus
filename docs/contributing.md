# Contributing to ProjectHephaestus

## Development Guidelines

Follow the principles outlined in [CLAUDE.md](../CLAUDE.md):

1. **KISS** - Keep It Simple, Stupid
2. **YAGNI** - You Ain't Gonna Need It
3. **DRY** - Don't Repeat Yourself
4. **SOLID** Principles
5. **Modularity** - Develop independent modules with well-defined interfaces

## Code Style

- Follow PEP 8 style guidelines
- Use type hints for all functions
- Provide comprehensive docstrings
- Include comprehensive error handling
- Write unit tests for all functionality

## Adding New Utilities

1. Determine the appropriate module (config, logging, io, utils, cli)
2. Create the utility function with proper type hints
3. Add comprehensive docstrings following Google Python Style Guide
4. Write unit tests for the new functionality
5. Update documentation

## Testing

All utility functions must include comprehensive test coverage:

```bash
# Run all tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=hephaestus --cov-report=html
```

## Submitting Changes

1. Create a feature branch
2. Make changes and commit with conventional commit messages
3. Ensure all tests pass
4. Submit a pull request
