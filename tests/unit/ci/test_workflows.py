"""Behavioral tests for :mod:`hephaestus.ci.workflows` using fixture trees."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.ci.workflows import (
    Violation,
    _is_checkout_step,
    _is_local_reference_step,
    check_inventory,
    collect_workflow_files,
    collect_yml_files,
    parse_readme_table,
    validate_workflow,
)


def test_collect_yml_files_excludes_worktrees(tmp_path: Path) -> None:
    """Only fixture workflows under the supplied repository root are collected."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI")
    nested = tmp_path / "worktrees" / "branch" / ".github" / "workflows"
    nested.mkdir(parents=True)
    (nested / "copy.yml").write_text("name: copy")

    assert collect_yml_files(tmp_path) == {"ci.yml"}


def test_parse_readme_table_ignores_non_table_text(tmp_path: Path) -> None:
    """Only workflow inventory table rows are parsed."""
    readme = tmp_path / "README.md"
    readme.write_text("ci.yml is mentioned here\n| [release.yml](#release) | Release |\n")

    assert parse_readme_table(readme) == {"release.yml"}


def test_check_inventory_reports_fixture_drift(tmp_path: Path) -> None:
    """Missing and undocumented fixture workflows are both reported."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI")
    (workflows / "README.md").write_text("| release.yml | Release |\n")

    assert check_inventory(tmp_path) == (["ci.yml"], ["release.yml"])


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ({"uses": "actions/checkout@v4"}, True),
        ({"uses": "actions/setup-python@v4"}, False),
        ({"run": "echo hello"}, False),
    ],
)
def test_is_checkout_step(step: object, expected: bool) -> None:
    """Checkout detection accepts only checkout action steps."""
    assert _is_checkout_step(step) is expected


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ({"uses": "./.github/actions/setup"}, True),
        ({"uses": "./.github/workflows/reusable.yml"}, True),
        ({"uses": "actions/checkout@v4"}, False),
    ],
)
def test_is_local_reference_step(step: object, expected: bool) -> None:
    """Local action and reusable-workflow references are detected."""
    assert _is_local_reference_step(step) is expected


def test_validate_workflow_reports_missing_checkout(tmp_path: Path) -> None:
    """A fixture job using a local action needs an earlier checkout step."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text("jobs:\n  test:\n    steps:\n      - uses: ./.github/actions/setup\n")

    violations = validate_workflow(workflow)

    assert len(violations) == 1
    assert isinstance(violations[0], Violation)
    assert violations[0].job_name == "test"


def test_collect_workflow_files_deduplicates_fixture_paths(tmp_path: Path) -> None:
    """Repeated fixture paths are returned once."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text("name: CI")

    assert collect_workflow_files([str(workflow), str(workflow)]) == [workflow]
