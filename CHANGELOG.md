# Changelog

All notable changes to ProjectHephaestus are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-03-17

### Added

- `write_secure(filepath, content, permissions=0o600)` in `hephaestus.io.utils` — writes files with restrictive permissions (e.g. credential files)
- New `hephaestus.github.rate_limit` module with:
  - `parse_reset_epoch(time_str, tz)` — parses GitHub CLI rate-limit reset times into epoch seconds
  - `detect_rate_limit(text)` — scans text for rate-limit messages and returns the reset epoch
  - `wait_until(epoch)` — blocks with countdown display until the given timestamp
  - `RATE_LIMIT_RE` regex and `ALLOWED_TIMEZONES` constant
- All new functions exposed via lazy imports on `hephaestus.*`
- Comprehensive test coverage for all new utilities

## [0.3.2] - 2026-03-17

### Fixed

- Changed `write_file`, `safe_write`, `ensure_directory`, and `save_data` to return `None` instead of `True` — these functions raise on failure, so the bool return was misleading
- Removed unnecessary `cast(str | bytes, ...)` in `read_file` (`io/utils.py`)
- Fixed cache key format inconsistency between `test.yml` and `release.yml` workflows
- Updated `docs/README.md` subpackage count to match actual structure
- Fixed project URLs in `pyproject.toml` to point to `HomericIntelligence/ProjectHephaestus`

### Added

- Tag-version consistency check in release workflow — prevents publishing when git tag doesn't match `pyproject.toml` version
- GitHub Release with artifacts created automatically on tag push
- `COMPATIBILITY.md` documenting backwards compatibility policy for v0.x and planned v1.0
- Python 3.10 and 3.11 classifiers plus `Topic` classifiers in `pyproject.toml`

### Changed

- Converted `__init__.py` from eager imports to lazy loading via PEP 562 (`__getattr__`) for faster `import hephaestus`
- CI coverage threshold aligned to 80% across `test.yml`, `release.yml`, and `pyproject.toml`
- Coverage upload limited to single matrix entry (ubuntu/3.12) to avoid duplicate reports

## [0.3.1] - 2026-03-15

### Fixed

- Fixed 21 bare `except Exception` clauses — all now justified with inline comments
- Fixed 35 f-string logging anti-patterns — replaced with `%`-style lazy formatting
- Fixed 44 unjustified `print()` calls in library code — replaced with `logging`
- Added `needs: test` gate to release workflow (prevents publishing on test failure)
- Aligned Python classifiers in `pyproject.toml` with CI (only `3.12` claimed)
- Documented CLI entry points in `README.md`

### Added

- Shared `find_markdown_files()` utility eliminating DRY violation across markdown subpackage
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1)

### Changed

- Aligned pytest coverage threshold to 80% in both `pyproject.toml` and CI
- Removed empty directories: `scripts/testing/`, `scripts/utilities/`
- Removed redundant `import re as _re` in `markdown/link_fixer.py`

## [0.3.0] - 2026-03-13

### Added

- `pyproject.toml` with Hatchling build backend replacing legacy `setup.py`
- BSD 3-Clause `LICENSE` file
- `.gitattributes` for consistent line-ending normalization
- `.yamllint.yaml` and `.markdownlint.json` linting configuration
- `CHANGELOG.md` (this file)
- GitHub PR template, issue templates, dependabot, and CODEOWNERS
- Three focused CI/CD workflows: `test.yml`, `pre-commit.yml`, `security.yml`
- Comprehensive test suite covering previously untested modules

### Changed

- Package name standardized to `hephaestus` (was `projecthephaestus` / `project-hephaestus`)
- Version bumped to `0.3.0`; now sourced from package metadata via `importlib.metadata`
- `pixi.toml` rewritten to use `[workspace]` header, editable self-install, and aligned environments
- Python minimum version raised to `>=3.10` (matching ProjectScylla)
- Linting migrated from black + flake8 to Ruff; mypy strict mode enabled
- Pre-commit hooks upgraded to ~15 comprehensive hooks matching ProjectScylla
- `.gitignore` expanded from 3 lines to comprehensive coverage
- `hephaestus/io/utils.py`: replaced unsafe pickle deserialization with explicit opt-in guard,
  replaced `print()` error reporting with `logging`, moved `yaml` import to call sites
- `hephaestus/cli/utils.py`: version string now sourced from `hephaestus.__version__`

### Removed

- `setup.py` (replaced by `pyproject.toml`)
- `requirements.txt` and `requirements-dev.txt` (deps consolidated in `pyproject.toml`)
- `pytest.ini` (config consolidated in `pyproject.toml`)
- One-time maintenance files: `ACTION_PLAN.md`, `CICD_IMPLEMENTATION_SUMMARY.md`,
  `CI_CD_SETUP.md`, `IMPLEMENTATION_SUMMARY.md`, `MANUAL_CLEANUP.sh`, `PIXI_USAGE.md`,
  `TEST_QUICK_START.md`, `run_cleanup_and_test.py`, `validate_cicd.py`
- Old `.github/workflows/ci.yml` (replaced by three focused workflows)

## [0.2.0] - 2024-02-12

### Added

- Ported utilities from ProjectOdyssey: markdown fixer, retry utilities, system info,
  dataset downloader
- Consolidated `src/hephaestus` into `hephaestus/`
- CI/CD pipeline with Pixi and pre-commit hooks

## [0.1.0] - 2024-02-10

### Added

- Initial repository structure
- Core utility modules: utils, config, io, cli, logging
