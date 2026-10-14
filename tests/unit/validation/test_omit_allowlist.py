"""Test the exact coverage omit list and current queue coverage floors."""

import tomllib
from pathlib import Path

_ALLOWED_OMITS = ["*/tests/*", "*/__init__.py"]

_QUEUE_CUTOVER_FLOORS = {
    "automation/implementer.py",
    "automation/planner.py",
    "automation/loop_runner.py",
    "automation/loop_repo_manager.py",
    "automation/address_review_core.py",
    "automation/pipeline_cli.py",
}

_RETIRED_FLOORS = {
    "automation/arming_state.py",
    "automation/ci_driver.py",
    "automation/pr_discovery.py",
    "automation/ci_check_inspector.py",
    "automation/post_merge_processor.py",
    "automation/curses_ui.py",
    "automation/audit_reviewer.py",
    "automation/address_review.py",
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


def test_queue_cutover_preserves_surviving_line_floors() -> None:
    """The surviving cohort and current CLI retain explicit line floors."""
    root = get_pyproject_toml_path().parent
    with open(root / "coverage.toml", "rb") as stream:
        modules = tomllib.load(stream)["coverage"]["modules"]

    assert set(modules) >= _QUEUE_CUTOVER_FLOORS
    for module in _QUEUE_CUTOVER_FLOORS:
        assert modules[module] == {"minimum": 70, "metric": "line"}

    assert all((root / "hephaestus" / module).is_file() for module in _QUEUE_CUTOVER_FLOORS)
    assert _RETIRED_FLOORS.isdisjoint(modules)
    assert all(not (root / "hephaestus" / module).exists() for module in _RETIRED_FLOORS)
