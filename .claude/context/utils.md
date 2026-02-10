# Python Utilities Context

This directory contains shared utility functions for the HomericIntelligence ecosystem.

## Key Guidelines

1. **Pure Functions**: Utilities should be stateless when possible
2. **Type Hints**: All functions must include complete type annotations
3. **Error Handling**: Robust error handling with meaningful messages
4. **Testing**: Every utility function must have corresponding unit tests
5. **Documentation**: Clear docstrings following Google Python Style Guide

## Common Patterns

- `validate_*` functions for input validation
- `format_*` functions for data transformation
- `calculate_*` functions for computations
- `extract_*` functions for data extraction
- `convert_*` functions for type conversion

## Dependencies

Utilities should minimize external dependencies. When dependencies are needed:
1. Use standard library when possible
2. Prefer widely-used, stable packages
3. Document all dependencies in function docstrings
4. Handle missing dependencies gracefully
