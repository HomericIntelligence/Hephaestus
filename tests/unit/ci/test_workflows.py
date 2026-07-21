"""Behavioral tests for :mod:`hephaestus.ci.workflows` using fixture trees."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

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

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REQUIRED_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "_required.yml"


def _required_workflow() -> dict[str, Any]:
    """Load the required workflow, preserving GitHub Actions' ``on`` key."""
    workflow = yaml.safe_load(_REQUIRED_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def test_pr_policy_keeps_independent_signature_validation() -> None:
    """The CI policy must retain a signature backstop beside the live ruleset."""
    jobs = _required_workflow()["jobs"]
    pr_policy = jobs["pr-policy"]
    steps = pr_policy["steps"]

    fetch = next(step for step in steps if step.get("id") == "fetch")
    assert "oid" in fetch["run"]
    assert "signature { isValid state }" in fetch["run"]

    signature_step = next(
        step for step in steps if step.get("name") == "Check 2: every commit is signed"
    )
    assert "signature.isValid // false" in signature_step["run"]


def test_collect_yml_files_excludes_worktrees(tmp_path: Path) -> None:
    """Only fixture workflows under the supplied repository root are collected."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI")
    nested = tmp_path / "worktrees" / "branch" / ".github" / "workflows"
    nested.mkdir(parents=True)
    (nested / "copy.yml").write_text("name: copy")

    assert collect_yml_files(tmp_path) == {"ci.yml"}


def test_collect_yml_files_finds_fixture_workflows(tmp_path: Path) -> None:
    """Workflow collection returns every fixture YAML file in its root."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI")
    (workflows / "release.yml").write_text("name: Release")

    assert collect_yml_files(tmp_path) == {"ci.yml", "release.yml"}


def test_collect_yml_files_returns_empty_without_workflow_directory(tmp_path: Path) -> None:
    """A fixture repository without workflows has no inventory entries."""
    assert collect_yml_files(tmp_path) == set()


def test_parse_readme_table_ignores_non_table_text(tmp_path: Path) -> None:
    """Only workflow inventory table rows are parsed."""
    readme = tmp_path / "README.md"
    readme.write_text("ci.yml is mentioned here\n| [release.yml](#release) | Release |\n")

    assert parse_readme_table(readme) == {"release.yml"}


def test_parse_readme_table_parses_plain_filename(tmp_path: Path) -> None:
    """Plain table filenames are included in the fixture inventory."""
    readme = tmp_path / "README.md"
    readme.write_text("| ci.yml | Runs tests |\n")

    assert parse_readme_table(readme) == {"ci.yml"}


def test_parse_readme_table_returns_empty_for_missing_file(tmp_path: Path) -> None:
    """A missing fixture README produces no documented-workflow entries."""
    assert parse_readme_table(tmp_path / "nonexistent.md") == set()


def test_check_inventory_reports_fixture_drift(tmp_path: Path) -> None:
    """Missing and undocumented fixture workflows are both reported."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI")
    (workflows / "README.md").write_text("| release.yml | Release |\n")

    assert check_inventory(tmp_path) == (["ci.yml"], ["release.yml"])


def test_check_inventory_accepts_synchronized_fixture(tmp_path: Path) -> None:
    """Matching fixture workflow and README inventories are accepted."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI")
    (workflows / "README.md").write_text("| ci.yml | CI |\n")

    assert check_inventory(tmp_path) == ([], [])


def test_check_inventory_reports_undocumented_fixture(tmp_path: Path) -> None:
    """An on-disk fixture omitted from its README is reported."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI")
    (workflows / "new.yml").write_text("name: New")
    (workflows / "README.md").write_text("| ci.yml | CI |\n")

    assert check_inventory(tmp_path) == (["new.yml"], [])


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ({"uses": "actions/checkout@v4"}, True),
        ({"uses": "actions/checkout"}, True),
        ({"uses": "actions/setup-python@v4"}, False),
        ({"run": "echo hello"}, False),
        ("not a dict", False),
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
        ({"run": "echo hi"}, False),
        ("not a dict", False),
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


def test_validate_workflow_accepts_checkout_before_local_action(tmp_path: Path) -> None:
    """A local action after checkout satisfies the checkout-first invariant."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  test:\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: ./.github/actions/setup\n"
    )

    assert validate_workflow(workflow) == []


def test_validate_workflow_accepts_fixture_without_jobs(tmp_path: Path) -> None:
    """Metadata-only workflow fixtures have no checkout-order violations."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text("name: empty\n")

    assert validate_workflow(workflow) == []


def test_validate_workflow_skips_large_fixture(tmp_path: Path) -> None:
    """Oversized workflow files are skipped before parsing."""
    workflow = tmp_path / "large.yml"
    workflow.write_bytes(b"x" * (1_048_576 + 1))

    assert validate_workflow(workflow) == []


def test_collect_workflow_files_deduplicates_fixture_paths(tmp_path: Path) -> None:
    """Repeated fixture paths are returned once."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text("name: CI")

    assert collect_workflow_files([str(workflow), str(workflow)]) == [workflow]


def test_collect_workflow_files_accepts_fixture_directory(tmp_path: Path) -> None:
    """Directory inputs collect both YAML filename extensions."""
    (tmp_path / "ci.yml").write_text("name: CI")
    (tmp_path / "release.yaml").write_text("name: Release")

    assert {path.name for path in collect_workflow_files([str(tmp_path)])} == {
        "ci.yml",
        "release.yaml",
    }


def test_collect_workflow_files_warns_for_missing_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing path reports a warning rather than failing collection."""
    assert collect_workflow_files([str(tmp_path / "missing.yml")]) == []
    assert "WARNING" in capsys.readouterr().err


def test_inventory_cli_returns_success_for_synchronized_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inventory CLI returns zero for an in-sync explicit repository."""
    from hephaestus.ci.workflows import check_workflow_inventory_main

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI")
    (workflows / "README.md").write_text("| ci.yml | CI workflow |\n")
    monkeypatch.setattr(
        "sys.argv", ["hephaestus-check-workflow-inventory", "--repo-root", str(tmp_path)]
    )

    assert check_workflow_inventory_main() == 0


def test_inventory_cli_returns_failure_for_drifting_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inventory CLI returns one when fixture inventory drifts."""
    from hephaestus.ci.workflows import check_workflow_inventory_main

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI")
    (workflows / "README.md").write_text("| other.yml | Other |\n")
    monkeypatch.setattr(
        "sys.argv", ["hephaestus-check-workflow-inventory", "--repo-root", str(tmp_path)]
    )

    assert check_workflow_inventory_main() == 1


def test_inventory_cli_uses_detected_fixture_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inventory CLI preserves its no-argument repository-root behavior."""
    from hephaestus.ci.workflows import check_workflow_inventory_main

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI")
    (workflows / "README.md").write_text("| ci.yml | CI workflow |\n")
    monkeypatch.setattr("hephaestus.utils.helpers.get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["hephaestus-check-workflow-inventory"])

    assert check_workflow_inventory_main() == 0


def test_checkout_validator_cli_returns_success_for_valid_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkout-validation CLI returns zero for a compliant workflow."""
    from hephaestus.ci.workflows import validate_workflow_checkout_main

    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  build:\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: ./.github/actions/setup\n"
    )
    monkeypatch.setattr("sys.argv", ["hephaestus-validate-workflow-checkout", str(workflow)])

    assert validate_workflow_checkout_main() == 0
