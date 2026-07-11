"""Regression tests for shared validation CLI parser adoption."""

from __future__ import annotations

from pathlib import Path

VALIDATION_MODULES = {
    "audit.py": 1,
    "cli_tier_docs.py": 1,
    "complexity.py": 1,
    "coverage.py": 1,
    "doc_config.py": 1,
    "doc_policy.py": 1,
    "docstrings.py": 1,
    "markdown.py": 2,
    "mypy_per_file.py": 1,
    "python_version.py": 1,
    "schema.py": 1,
    "stale_scripts.py": 1,
    "test_structure.py": 1,
    "tier_labels.py": 1,
    "type_aliases.py": 1,
}

# Modules skipped by the resolver guard are those without a
# "create_validation_parser(" call or with "include_repo_root=False"
# (no --repo-root flag means no resolver duplication is possible).
# 11 of the 17 modules above currently qualify; the floor guards against
# API drift (e.g. a rename of create_validation_parser) silently turning
# the resolver check into a no-op that passes vacuously.
MIN_RESOLVER_CHECKED_MODULES = 10


def test_issue_1409_validation_clis_use_shared_parser() -> None:
    """Issue #1409 validation entry points use the canonical parser helper."""
    root = Path(__file__).resolve().parents[3]
    for filename, expected_calls in VALIDATION_MODULES.items():
        text = (root / "hephaestus" / "validation" / filename).read_text()
        assert text.count("create_validation_parser(") == expected_calls, filename
        assert "add_json_arg" not in text, filename
        assert "add_version_arg" not in text, filename
        assert 'add_argument("--repo-root"' not in text, filename


def test_issue_1413_validation_clis_use_shared_repo_root_resolver() -> None:
    """Validation entry points should not duplicate repo-root fallback logic."""
    root = Path(__file__).resolve().parents[3]
    checked = 0
    for filename in VALIDATION_MODULES:
        text = (root / "hephaestus" / "validation" / filename).read_text()
        if "create_validation_parser(" not in text or "include_repo_root=False" in text:
            continue
        checked += 1
        assert "resolve_repo_root(args)" in text, filename
        assert "args.repo_root or get_repo_root()" not in text, filename
        assert "args.repo_root if args.repo_root is not None else get_repo_root()" not in text, (
            filename
        )
    assert checked >= MIN_RESOLVER_CHECKED_MODULES, (
        f"only {checked} modules were checked; the skip conditions above may have "
        "drifted out of sync with the create_validation_parser API"
    )
