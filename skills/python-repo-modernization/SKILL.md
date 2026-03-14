# Python Repository Modernization

## Overview

| Attribute | Value |
|-----------|-------|
| **Date** | 2026-03-13 |
| **Objective** | Bring a partially modernized Python repo to production-grade quality: fix bugs, restructure tests, enhance CI/pre-commit, add PEP 561 marker, prepare for PyPI publishing |
| **Outcome** | ✅ 184 tests pass, 61% coverage, all imports verified, release workflow created |
| **Project** | ProjectHephaestus v0.3.0 (Hatchling + Pixi + ruff/mypy) |

## When to Use

Use this skill when you need to:
- Fix circular imports in a Python package
- Remove backward-compat shims no longer needed
- Restructure flat `tests/` into `tests/unit/<subpackage>/` mirroring source layout
- Add a PEP 561 `py.typed` marker for downstream type consumers
- Harden pre-commit hooks (CVE scanning, lock freshness, structure enforcement, complexity)
- Enhance GitHub Actions CI with matrix strategy and codecov flags
- Create a PyPI release workflow triggered by semver tags
- Fix stale documentation referencing renamed functions or removed modules

**Triggers:**
- "Bring this repo to production grade"
- "Match the quality bar of [other repo]"
- "Prepare for PyPI publishing"
- "Restructure tests to mirror package layout"
- "Fix circular imports in `__init__.py`"

## Verified Workflow

### 1. Fix Circular Imports

When a submodule imports from the package's own `__init__.py`, use `importlib.metadata` directly:

```python
# ❌ Circular: hephaestus/cli/utils.py
from hephaestus import __version__

# ✅ Direct importlib.metadata
from importlib.metadata import PackageNotFoundError, version as _pkg_version
try:
    __version__ = _pkg_version("hephaestus")
except PackageNotFoundError:
    __version__ = "0.3.0"
```

### 2. Fix Type Hint for Optional List Parameters

```python
# ❌ Wrong — default None is not a valid list[float]
retry_delays: list[float] = None

# ✅ Correct
retry_delays: list[float] | None = None
```

### 3. Fix Boolean Logic in Type Coercion

When detecting floats from string env vars, the condition must check for the presence of `.`, not its absence:

```python
# ❌ Original — backwards, tries float when no dot present
if '.' not in value and value.isdigit():
    value = int(value)
elif '.' not in value:           # ← This also matches non-float strings
    value = float(value)

# ✅ Fixed — use a separate typed variable, don't reassign value
typed_value: int | float | str = value
try:
    if '.' not in value and value.isdigit():
        typed_value = int(value)
    elif '.' in value:
        typed_value = float(value)
except ValueError:
    pass
current[keys[-1]] = typed_value  # store typed value, not str
```

### 4. Add PEP 561 Marker

```bash
touch hephaestus/py.typed
```

This empty file signals to mypy and other type checkers that the package ships inline types.

### 5. Delete Backward-Compat Shims

When a shim module only re-exports from the canonical location and nothing imports it:

```bash
rm -rf hephaestus/helpers/
```

Verify no remaining references:
```bash
grep -r "hephaestus.helpers" .  # should return nothing
```

### 6. Restructure Tests to Mirror Package Layout

```
tests/
  __init__.py
  unit/
    __init__.py
    cli/
      __init__.py
      test_colors.py
      test_utils.py
    config/
      __init__.py
      test_utils.py
    utils/
      __init__.py
      test_general_utils.py
    ...  (one subdir per hephaestus/ subpackage)
```

Move files with `cp`, then delete originals. Add `__init__.py` to every new directory. Update `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests/unit"]
pythonpath = [".", "scripts"]
```

### 7. Add Structure Enforcement Script

Create `scripts/check_unit_test_structure.py` that verifies every `hephaestus/<subpackage>` has a matching `tests/unit/<subpackage>/` directory. Wire it as a pre-commit hook:

```yaml
- id: check-unit-test-structure
  name: Check unit test structure
  entry: python scripts/check_unit_test_structure.py
  language: system
  pass_filenames: false
  files: ^(hephaestus|tests/unit)/
```

### 8. Harden Pre-commit Hooks

Add to `.pre-commit-config.yaml`:

```yaml
# CVE scanning (manual stage to avoid blocking every commit)
- id: pip-audit
  name: pip-audit (CVE scan)
  entry: pixi run pip-audit
  language: system
  pass_filenames: false
  stages: [manual]

# Lock file freshness
- id: check-pixi-lock
  name: Check pixi lock file
  entry: pixi install --locked
  language: system
  pass_filenames: false
  files: ^pixi\.(toml|lock)$

# Complexity enforcement
- id: ruff-check-complexity
  name: Ruff Complexity Check (C901)
  entry: pixi run ruff check --select C901 hephaestus/
  language: system
  files: ^hephaestus/.*\.py$
  types: [python]
  pass_filenames: false
```

### 9. Enhance test.yml CI

Add matrix strategy (extensible to integration tests later) and codecov flags:

```yaml
strategy:
  matrix:
    test-type: [unit]

# Use hardcoded path to avoid matrix injection risk
- name: Run unit tests
  run: |
    pixi run pytest tests/unit ...

- name: Upload coverage
  uses: codecov/codecov-action@v4
  with:
    flags: unit
```

**Security note:** Do NOT use `${{ matrix.test-type }}` in `run:` commands — hardcode the test path. Matrix values from the same workflow file are safe in `name:` fields but using them in `run:` is a command injection risk if the matrix ever accepts external input.

### 10. Add PyPI Release Workflow

Trigger on semver tags, use Trusted Publishing (OIDC) pattern:

```yaml
on:
  push:
    tags:
      - "v*"

permissions:
  contents: read
  id-token: write

- name: Build package
  run: pixi run python -m build

- name: Publish to PyPI
  uses: pypa/gh-action-pypi-publish@release/v1
  with:
    password: ${{ secrets.PYPI_API_TOKEN }}
```

### 11. Fix README/CLAUDE.md Stale References

Common stale patterns to find and fix:

| Stale | Correct |
|-------|---------|
| `hephaestus.utils.general` | `hephaestus.utils` |
| `run_command` | `run_subprocess` |
| `get_nested_value` | `get_setting` |
| `pixi run docs-build` | (remove — task doesn't exist) |
| `Python 3.8+` | `Python 3.10+` |
| `requirements.txt` / `requirements-dev.txt` | `pixi.toml` / `pyproject.toml` |
| `flake8` / `black` / `tox` | `ruff` / `pixi run` |

## Failed Attempts

### ❌ Float Coercion — Reassigning `value` to `str(converted)`

**What happened:** Initial fix for the float logic stored the converted value back as a string (`value = str(converted)`), which preserved the bug's end-effect (everything stored as string).

**Why it failed:** `value` is a `str` from `os.environ.items()`. Reassigning it to `str(converted)` means the nested config key still gets a string, not an int/float.

**Fix:** Introduce a separate `typed_value: int | float | str` variable and assign that to the config dict — never touch `value`.

### ❌ Using `${{ matrix.test-type }}` in `run:` commands

**What happened:** Initially templated the test path with the matrix variable in the `run:` step.

**Why avoided:** GitHub Actions security scanners flag matrix values in `run:` as potential injection vectors (even if controlled). Hardcoding `tests/unit` in the run step is cleaner and avoids the security warning from the pre-bash-exec hook.

### ❌ `rmdir` after deleting only Python files

**What happened:** `rmdir hephaestus/helpers` failed because `__pycache__` remained.

**Fix:** Always use `rm -rf` for directories with Python files, since `__pycache__` is always present after any import.

## Results & Parameters

### Final State

```
184 tests passed in 2.99s
Coverage: 61.04% (≥50% threshold met)
hephaestus.__version__ = "0.3.0"
from hephaestus import slugify, retry_with_backoff  # ✅
python scripts/check_unit_test_structure.py         # ✅ 13/13 subpackages
```

### Key pyproject.toml Settings

```toml
[tool.pytest.ini_options]
testpaths = ["tests/unit"]
pythonpath = [".", "scripts"]
addopts = ["-v", "--strict-markers", "--cov=hephaestus",
           "--cov-report=term-missing", "--cov-report=html",
           "--cov-fail-under=50"]

[tool.hatch.build.targets.wheel]
packages = ["hephaestus"]  # picks up py.typed automatically
```

### Pre-commit Hook Summary

| Hook ID | Purpose | Stage |
|---------|---------|-------|
| `check-shell-injection` | Prevent `shell=True` | commit |
| `ruff-format-python` | Auto-format | commit |
| `ruff-check-python` | Lint + fix | commit |
| `mypy-check-python` | Type check | commit |
| `pip-audit` | CVE scan | manual |
| `check-pixi-lock` | Lock freshness | commit |
| `check-unit-test-structure` | Test layout mirror | commit |
| `ruff-check-complexity` | C901 complexity | commit |
| `markdownlint-cli2` | Markdown lint | commit |
| `yamllint` | YAML lint | commit |

## Checklist for Future Use

- [ ] Search for circular imports: `grep -r "from hephaestus import" hephaestus/`
- [ ] Fix `param: list[X] = None` → `param: list[X] | None = None`
- [ ] Review type coercion logic for off-by-one boolean errors
- [ ] Delete shim modules that only re-export from canonical location
- [ ] `touch <package>/py.typed`
- [ ] Create `tests/unit/<subpackage>/` structure with `__init__.py` files
- [ ] Update `pyproject.toml` testpaths and pythonpath
- [ ] Write `scripts/check_unit_test_structure.py`
- [ ] Add pip-audit, check-pixi-lock, structure check, complexity hooks
- [ ] Create `.github/workflows/release.yml` for tag-triggered PyPI publish
- [ ] Fix stale README/docs references (function names, module paths, removed tasks)
- [ ] Verify: `pytest tests/unit -v` → all pass, coverage ≥ threshold
- [ ] Verify: `python -c "import <package>; print(<package>.__version__)"`

## Related Skills

- `github-actions-python-cicd` — Full CI/CD pipeline setup
- `create-reusable-utilities` — Porting utilities across projects
- `python-packaging` — Hatchling/pyproject.toml configuration
