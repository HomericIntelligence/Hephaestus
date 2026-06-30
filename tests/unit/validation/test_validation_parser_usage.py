"""Regression tests for shared validation CLI parser adoption."""

from __future__ import annotations

from pathlib import Path

VALIDATION_MODULES = {
    "audit.py": 1,
    "tiers/cli_tier_docs.py": 1,
    "code/complexity.py": 1,
    "coverage.py": 1,
    "docs/doc_config.py": 1,
    "docs/doc_policy.py": 1,
    "docs/docstrings.py": 1,
    "markdown.py": 2,
    "code/mypy_per_file.py": 1,
    "python_version.py": 1,
    "skills/repo_analyze_skills.py": 1,
    "schema.py": 1,
    "skills/skill_catalog.py": 1,
    "stale_scripts.py": 1,
    "test_structure.py": 1,
    "tiers/tier_labels.py": 1,
    "code/type_aliases.py": 1,
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
