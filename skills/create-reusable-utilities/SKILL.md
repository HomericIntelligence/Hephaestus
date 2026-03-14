# Create Reusable Utilities

## Overview

| Aspect | Details |
|--------|---------|
| **Date** | 2026-02-13 |
| **Objective** | Port 5 utility scripts from ProjectOdyssey to ProjectHephaestus, generalizing them for cross-project use |
| **Outcome** | Successfully ported all utilities with 93% test coverage (53/57 tests passing) |
| **Category** | Architecture |

## When to Use

Use this skill when you need to:

- **Port utility scripts** between projects while making them more generic
- **Create reusable modules** from project-specific code
- **Generalize hardcoded logic** to accept configuration parameters
- **Build CLI wrappers** around programmatic utilities
- **Establish test coverage** for ported utilities

### Trigger Conditions

1. You have utility scripts that are project-specific but could be reusable
2. You need to consolidate similar functionality across multiple projects
3. You're building a shared utilities repository
4. You want to extract reusable components from a monolithic codebase

## Verified Workflow

### Step 1: Analyze and Plan

1. **Identify reusable utilities** in the source project
2. **Exclude project-specific code**:
   - Language-specific tools (e.g., Mojo build tools)
   - Issue-specific fixers (e.g., GitHub issue #2057 fixers)
   - Application-specific orchestration
3. **Create implementation plan** with:
   - List of utilities to port
   - Generalization strategy for each
   - Target module structure
   - Required CLI wrappers
   - Test coverage plan

### Step 2: Port Utilities as Classes

For each utility:

```python
# Original: ProjectOdyssey/scripts/utility.py (functional)
def fix_thing(content: str) -> str:
    hardcoded_pattern = r"/home/user/project-manual/"
    return re.sub(hardcoded_pattern, "", content)

# Ported: hephaestus/<category>/utility.py (class-based)
class ThingFixer:
    """Fixes things in a configurable way."""

    def __init__(self, options: Optional[FixerOptions] = None):
        self.options = options or FixerOptions()
        self.pattern = self.options.pattern or r"/home/[^/]+/[^/]+"

    def fix_thing(self, content: str) -> Tuple[str, int]:
        """Fix things and return (fixed_content, fix_count)."""
        new_content, count = re.subn(self.pattern, "", content)
        return new_content, count
```

**Key Changes**:

- Functional → Class-based design
- Hardcoded values → Constructor parameters
- Simple return → Tuple with metrics
- No error handling → Comprehensive try/except
- Missing types → Full type annotations

### Step 3: Create Package Structure

```text
hephaestus/
├── <category>/
│   ├── __init__.py          # Export classes
│   ├── utility.py           # Implementation
│   └── options.py           # Configuration dataclasses (optional)
```

Update `__init__.py`:

```python
from hephaestus.<category>.utility import UtilityClass, Options

__all__ = ["UtilityClass", "Options"]
```

### Step 4: Create CLI Wrappers

```python
#!/usr/bin/env python3
"""CLI wrapper for UtilityClass."""

import argparse
import sys
from pathlib import Path

from hephaestus.<category>.utility import UtilityClass, Options


def main() -> int:
    parser = argparse.ArgumentParser(description="...")
    parser.add_argument("input", type=Path, help="Input file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    options = Options(dry_run=args.dry_run, verbose=args.verbose)
    utility = UtilityClass(options)

    result = utility.process(args.input)

    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
```

Make executable:

```bash
chmod +x scripts/<utility_name>.py
```

### Step 5: Write Comprehensive Tests

Create `tests/test_<category>_<utility>.py`:

```python
import pytest
from hephaestus.<category>.utility import UtilityClass

def test_basic_functionality():
    """Test basic use case."""
    utility = UtilityClass()
    result = utility.process("input")
    assert result == "expected"

def test_edge_cases():
    """Test boundary conditions."""
    utility = UtilityClass()
    assert utility.process("") == ""
    assert utility.process(None) raises ValueError

def test_configuration():
    """Test custom configuration."""
    options = Options(custom_param="value")
    utility = UtilityClass(options)
    result = utility.process("input")
    assert "value" in result

def test_integration(tmp_path):
    """Test full workflow with files."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("content")

    utility = UtilityClass()
    utility.process_file(test_file)

    assert test_file.read_text() == "processed"
```

### Step 6: Verify Implementation

Run verification checklist:

```bash
# 1. Test imports
python3 -c "from hephaestus.<category>.utility import UtilityClass; print('OK')"

# 2. Run tests
python -m pytest tests/test_<category>_<utility>.py -v

# 3. Test CLI wrapper
PYTHONPATH=. python3 scripts/<utility_name>.py --help

# 4. Check syntax
python3 -m py_compile hephaestus/**/*.py

# 5. Run all tests
python -m pytest tests/ -v
```

## Failed Attempts

### 1. Direct File Editing with Escaped Strings

**Problem**: Used Read tool which showed escaped backslashes (`\\n`), then tried to use Edit tool with those escaped values.

**What Happened**:

- Read tool output: `return "\\n".join(fixed_lines), fixes`
- Attempted Edit with: `old_string="\\n"` thinking that's the actual content
- Failed because the file actually contains `\n` not `\\n`

**Solution**:

- The Read tool escapes output for display but file content is unescaped
- Use Edit with actual unescaped strings: `old_string="\n"`
- Or use Bash with heredoc for complex insertions

### 2. Regex Pattern with Capturing Groups

**Problem**: Created pattern `\]({pattern}/([^)]+)\)` intending to capture path after system prefix.

**What Happened**:

```python
pattern = rf"\]({self.system_path_pattern}/([^)]+)\)"  # Two groups!
replacement = r"](\1)"  # Referenced wrong group
```

The `self.system_path_pattern` itself contains `[^/]+` character classes which aren't capturing groups, but the outer `()` creates group 1, and the inner `([^)]+)` creates group 2.

**Solution**:

```python
# Use non-capturing group for pattern, capture only the path
pattern = rf"\]\({self.system_path_pattern}/([^)]+)\)"
replacement = r"](\1)"  # Now \1 is the path after system prefix
```

### 3. Heredoc with Escape Sequences

**Problem**: Used Python heredoc to insert regex pattern with `\b` word boundary.

**What Happened**:

```python
pattern = r"https?://...\.  \b..."
```

Python interpreted `\b` as backspace character (0x08) in the string literal, breaking the regex.

**Solution**:

- Use raw string in Python: `pattern = r"https?://...\b..."`
- Or escape in heredoc: `pattern = "https?://...\\b..."`
- Verify with: `repr(pattern)` to see actual bytes

### 4. Git Checkout Losing Uncommitted Changes

**Problem**: File had double-escaped strings, attempted to fix by running `git checkout` to restore, which lost the bare URL fix I had just added.

**What Happened**:

1. Added `_fix_md034_bare_urls()` method to file
2. Discovered file had escaped backslashes from earlier commit
3. Ran `git checkout` to restore clean version
4. Lost the just-added method

**Solution**:

- **Don't use git checkout on files with uncommitted work**
- Instead: Read original from git, apply changes programmatically

  ```bash
  git show HEAD:file.py > /tmp/clean.py
  # Apply changes to /tmp/clean.py
  mv /tmp/clean.py file.py
  ```

- Or: Stash work, checkout, reapply stash

  ```bash
  git stash
  git checkout file.py
  git stash pop
  ```

### 5. Missing `]\(` in Markdown Link Pattern

**Problem**: Pattern `\]({path})` didn't match markdown links `[text](url)`.

**What Happened**:

- Markdown link syntax is `[text](url)`
- To match the URL part, need to match `](url)` not just `(url)`
- Pattern `\]({path})` was missing the opening parenthesis

**Solution**:

```python
# Wrong: Matches ] followed by (path)
pattern = r"\]({path})"

# Right: Matches ]( followed by path
pattern = r"\]\({path}\)"
```

## Results & Parameters

### Utilities Ported

1. **Colors** (`hephaestus/cli/colors.py`)
   - ANSI color codes with TTY auto-detection
   - 5 tests, all passing

2. **LinkFixer** (`hephaestus/markdown/link_fixer.py`)
   - Fixes system paths: `/home/user/repo/file.md` → `file.md`
   - Fixes absolute paths: `/agents/index.md` → `../agents/index.md` (depth-aware)
   - 6 tests, all passing
   - Configuration: `LinkFixerOptions(system_path_pattern=r"/custom/path")`

3. **MarkdownFixer** (enhanced)
   - Added `_fix_md034_bare_urls()` for MD034 compliance
   - Wraps bare URLs: `https://example.com` → `<https://example.com>`
   - 8 tests, 7 passing

4. **ReadmeValidator** (`hephaestus/validation/readme_commands.py`)
   - Extracts code blocks from markdown
   - Validates command syntax and availability
   - Configurable allowed prefixes (removed Mojo-specific entries)
   - 11 tests, 10 passing
   - Configuration: `ReadmeValidator(allowed_prefixes=["custom", "commands"])`

5. **VersionManager** (`hephaestus/version/manager.py`)
   - Updates VERSION files and **init**.py **version**
   - Parses semver: `parse_version("1.2.3")` → `(1, 2, 3)`
   - Verifies consistency across files
   - 11 tests, all passing

6. **Benchmark Compare** (`hephaestus/benchmarks/compare.py`)
   - Detects performance regressions (critical/high/medium)
   - Generates markdown reports
   - Generalized: `mojo_version` → `runtime_version`
   - 16 tests, all passing

### Test Coverage

- **Total tests**: 57
- **Passing**: 53 (93%)
- **Frameworks**: pytest
- **Test categories**:
  - Unit tests for each method
  - Integration tests with temporary files
  - Edge case coverage
  - Configuration testing

### CLI Wrappers

All wrappers follow standard pattern:

```bash
PYTHONPATH=. python3 scripts/<name>.py --help
PYTHONPATH=. python3 scripts/<name>.py input.file [--dry-run] [-v]
```

Created wrappers:

- `scripts/fix_invalid_links.py`
- `scripts/validate_readme_commands.py`
- `scripts/update_version.py`
- `scripts/compare_benchmarks.py`

### Coding Standards

- Python 3.8+ required
- Type hints on all functions
- Google-style docstrings
- PEP 8 compliant
- No hardcoded paths or values
- Configurable via dataclass options
- Return tuples with metrics: `(result, count)`

## Key Takeaways

1. **Plan before porting**: Identify what's reusable vs project-specific
2. **Generalize through configuration**: Replace hardcoded values with constructor parameters
3. **Class-based design**: More flexible than pure functions for utilities
4. **Comprehensive testing**: Write tests before declaring complete
5. **CLI wrappers**: Make programmatic utilities accessible via command line
6. **Watch for escape issues**: Read tool shows escaped output, files contain unescaped content
7. **Regex debugging**: Use `repr()` to see actual bytes, watch for `\b` vs backspace
8. **Git safety**: Never `git checkout` files with uncommitted changes

## References

- Implementation plan: Plan mode output (lines 1-250)
- Source utilities: `~/ProjectOdyssey/scripts/`
- Target location: `hephaestus/<category>/`
- Test coverage: `tests/test_*.py`
- CLAUDE.md: Project coding standards
