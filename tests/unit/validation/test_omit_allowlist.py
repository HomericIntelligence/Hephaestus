"""Test the exact coverage omit list and issue #2371 migration contract."""

import tomllib
from pathlib import Path

_ALLOWED_OMITS = ["*/tests/*", "*/__init__.py"]

_ISSUE_2371_FLOORS = {
    "automation/implementer.py",
    "automation/planner.py",
    "automation/ci_driver.py",
    "automation/pr_discovery.py",
    "automation/ci_check_inspector.py",
    "automation/ci_fix_orchestrator.py",
    "automation/post_merge_processor.py",
    "automation/loop_runner.py",
    "automation/loop_repo_manager.py",
    "automation/curses_ui.py",
    "automation/audit_reviewer.py",
    "automation/address_review_core.py",
}


def get_pyproject_toml_path() -> Path:
    """Find the project root and return path to pyproject.toml."""
    current = Path(__file__).resolve()
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current / "pyproject.toml"
        current = current.parent
    raise RuntimeError("Could not find pyproject.toml")


def test_omit_allowlist_is_exact() -> None:
    """Only generic test and package-init exclusions are permitted."""
    root = get_pyproject_toml_path().parent
    with open(root / "pyproject.toml", "rb") as stream:
        omit = tomllib.load(stream)["tool"]["coverage"]["run"]["omit"]

    assert omit == _ALLOWED_OMITS


def test_issue_2371_cohort_has_executable_line_floors() -> None:
    """Every issue cohort source has an explicit measured line floor."""
    root = get_pyproject_toml_path().parent
    with open(root / "coverage.toml", "rb") as stream:
        modules = tomllib.load(stream)["coverage"]["modules"]

    assert set(modules) >= _ISSUE_2371_FLOORS
    for module in _ISSUE_2371_FLOORS:
        assert modules[module] == {"minimum": 70, "metric": "line"}

    promoted = _ISSUE_2371_FLOORS - {"automation/address_review_core.py"}
    assert all((root / "hephaestus" / module).is_file() for module in promoted)
    assert not (root / "hephaestus/automation/address_review.py").exists()
    assert (root / "hephaestus/automation/address_review_core.py").is_file()
