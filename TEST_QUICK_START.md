# Quick Start: Running Tests

## TL;DR

```bash
# One command to rule them all
python run_cleanup_and_test.py
```

This script will:
1. Clean up obsolete files/directories
2. Verify installation
3. Run all tests

## Using Pixi (Recommended)

ProjectHephaestus uses Pixi for dependency management. All Python commands should use `pixi run`:

### 1. Install Pixi

```bash
curl -fsSL https://pixi.sh/install.sh | bash
```

### 2. Install Dependencies

```bash
pixi install
```

### 3. Run Tests

```bash
# All tests
pixi run pytest

# Verbose output
pixi run pytest -v

# With coverage
pixi run pytest --cov=hephaestus

# Specific file
pixi run pytest tests/test_general_utils.py

# Specific test
pixi run pytest tests/test_general_utils.py::test_slugify
```

### 4. Set Up Pre-commit

```bash
pixi run pre-commit install
```

## Alternative: Without Pixi

If you prefer traditional pip:

```bash
pip install -e .[dev]
pytest -v
```

## Common Commands

```bash
# Run only fast tests
pixi run pytest -m "not slow"

# Run only last failed
pixi run pytest --lf

# Stop on first failure
pixi run pytest -x

# Show print statements
pixi run pytest -s

# More verbose errors
pixi run pytest -vv --tb=long
```

## Check Test Coverage

```bash
# Terminal output
pixi run pytest --cov=hephaestus --cov-report=term-missing

# HTML report
pixi run pytest --cov=hephaestus --cov-report=html
# Then open htmlcov/index.html
```

## CI/CD Status

After pushing to GitHub, check:
- GitHub → Actions tab → View workflow runs
- Look for ✅ green checkmark or ❌ red X

## Troubleshooting

**Tests not found?**
```bash
# Make sure you're in project root
cd /home/mvillmow/ProjectHephaestus
pixi run pytest
```

**Import errors?**
```bash
pixi install
```

**Still failing?**
```bash
# Check what's wrong
pixi run python -c "import hephaestus; print('OK')"
pixi run python -c "import yaml; print('PyYAML OK')"
```

## Files to Know

- `pixi.toml` - Pixi configuration
- `pytest.ini` - Test configuration
- `.pre-commit-config.yaml` - Pre-commit hooks
- `tests/` - All test files
- `.github/workflows/ci.yml` - CI/CD pipeline
- `PIXI_USAGE.md` - Pixi command reference
- `CI_CD_SETUP.md` - Detailed guide
- `CICD_IMPLEMENTATION_SUMMARY.md` - What was done

## Need More Help?

- `PIXI_USAGE.md` - Pixi-specific commands and setup
- `CI_CD_SETUP.md` - Comprehensive testing documentation
