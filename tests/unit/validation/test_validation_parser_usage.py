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
    "repo_analyze_skills.py": 1,
    "schema.py": 1,
    "skill_catalog.py": 1,
    "stale_scripts.py": 1,
    "test_structure.py": 1,
    "tier_labels.py": 1,
    "type_aliases.py": 1,
}


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
    for filename in VALIDATION_MODULES:
        text = (root / "hephaestus" / "validation" / filename).read_text()
        if "create_validation_parser(" not in text or "include_repo_root=False" in text:
            continue
        assert "resolve_repo_root(args)" in text, filename
        assert "args.repo_root or get_repo_root()" not in text, filename
        assert "args.repo_root if args.repo_root is not None else get_repo_root()" not in text, (
            filename
        )
