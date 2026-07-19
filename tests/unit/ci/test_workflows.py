"""Tests for hephaestus.ci.workflows."""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no stdlib tomllib
    import tomli as tomllib

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

REPO_ROOT = Path(__file__).resolve().parents[3]
PERFORMANCE_DOC = REPO_ROOT / "docs" / "performance-testing.md"
SETUP_PI_ACTION = REPO_ROOT / ".github" / "actions" / "setup-pi-cli" / "action.yml"


class TestCollectYmlFiles:
    """Tests for collect_yml_files()."""

    def test_finds_yml_files(self, tmp_path: Path) -> None:
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "release.yml").write_text("name: Release")
        result = collect_yml_files(tmp_path)
        assert "ci.yml" in result
        assert "release.yml" in result

    def test_no_workflows_dir(self, tmp_path: Path) -> None:
        assert collect_yml_files(tmp_path) == set()

    def test_excludes_worktrees(self, tmp_path: Path) -> None:
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        # Worktree path — create a worktrees subdir
        worktree_wf = tmp_path / "worktrees" / "branch" / ".github" / "workflows"
        worktree_wf.mkdir(parents=True)
        (worktree_wf / "ci.yml").write_text("name: CI (worktree copy)")
        result = collect_yml_files(tmp_path)
        # Only one ci.yml should appear (from main .github/workflows/)
        assert "ci.yml" in result
        assert len([f for f in result if f == "ci.yml"]) == 1


class TestParseReadmeTable:
    """Tests for parse_readme_table()."""

    def test_parses_plain_filename(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("| ci.yml | Runs tests |\n")
        result = parse_readme_table(readme)
        assert "ci.yml" in result

    def test_parses_linked_filename(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("| [release.yml](#release) | Creates releases |\n")
        result = parse_readme_table(readme)
        assert "release.yml" in result

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = parse_readme_table(tmp_path / "nonexistent.md")
        assert result == set()

    def test_ignores_non_table_lines(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("# Workflows\n\nThis repo uses ci.yml for testing.\n")
        result = parse_readme_table(readme)
        assert "ci.yml" not in result


class TestCheckInventory:
    """Tests for check_inventory()."""

    def _setup(self, tmp_path: Path, on_disk: list[str], in_readme: list[str]) -> None:
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        for name in on_disk:
            (workflows / name).write_text(f"name: {name}")
        readme = workflows / "README.md"
        table_rows = "\n".join(f"| {name} | desc |" for name in in_readme)
        readme.write_text(f"# Workflows\n\n{table_rows}\n")

    def test_in_sync(self, tmp_path: Path) -> None:
        self._setup(tmp_path, ["ci.yml"], ["ci.yml"])
        undoc, missing = check_inventory(tmp_path)
        assert undoc == []
        assert missing == []

    def test_undocumented_file(self, tmp_path: Path) -> None:
        self._setup(tmp_path, ["ci.yml", "new.yml"], ["ci.yml"])
        undoc, _missing = check_inventory(tmp_path)
        assert "new.yml" in undoc

    def test_missing_file(self, tmp_path: Path) -> None:
        self._setup(tmp_path, ["ci.yml"], ["ci.yml", "phantom.yml"])
        _, missing = check_inventory(tmp_path)
        assert "phantom.yml" in missing


class TestWorkflowInventoryLiveTree:
    """Live-tree regression tests for workflow inventory enforcement."""

    def test_workflow_inventory_hook_is_wired_in_precommit(self) -> None:
        from hephaestus.ci.precommit import load_precommit_config

        repos = load_precommit_config(REPO_ROOT / ".pre-commit-config.yaml")
        hook = None
        for repo in repos:
            hooks = repo.get("hooks")
            if not isinstance(hooks, list):
                continue
            for candidate in hooks:
                if isinstance(candidate, dict) and candidate.get("id") == (
                    "hephaestus-check-workflow-inventory"
                ):
                    hook = candidate
                    break
            if hook is not None:
                break

        assert hook is not None
        assert hook["entry"] == "uv run hephaestus-check-workflow-inventory"
        assert hook["pass_filenames"] is False
        assert hook["always_run"] is True
        assert (
            hook["files"]
            == r"^(\.pre-commit-config\.yaml|\.github/workflows/(README\.md|.*\.yml))$"
        )


class TestPerformanceWorkflow:
    """Contracts for the bounded worker-pool performance lane."""

    def test_performance_strategy_is_documented(self) -> None:
        """The public docs index links to the performance strategy."""
        index = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
        assert PERFORMANCE_DOC.is_file()
        assert "(performance-testing.md)" in index

    def test_default_pytest_options_deselect_performance_tests(self) -> None:
        """Normal test runs do not accidentally execute the stress lane."""
        config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
        assert "-m" in addopts
        assert "not performance" in addopts


class TestIsCheckoutStep:
    """Tests for _is_checkout_step()."""

    def test_checkout_step(self) -> None:
        assert _is_checkout_step({"uses": "actions/checkout@v4"}) is True

    def test_checkout_without_version(self) -> None:
        assert _is_checkout_step({"uses": "actions/checkout"}) is True

    def test_non_checkout(self) -> None:
        assert _is_checkout_step({"uses": "actions/setup-python@v4"}) is False

    def test_not_dict(self) -> None:
        assert _is_checkout_step("not a dict") is False

    def test_no_uses_key(self) -> None:
        assert _is_checkout_step({"run": "echo hello"}) is False


class TestIsLocalReferenceStep:
    """Tests for _is_local_reference_step()."""

    def test_local_action(self) -> None:
        assert _is_local_reference_step({"uses": "./.github/actions/setup"}) is True

    def test_local_workflow(self) -> None:
        assert _is_local_reference_step({"uses": "./.github/workflows/reusable.yml"}) is True

    def test_external_action(self) -> None:
        assert _is_local_reference_step({"uses": "actions/checkout@v4"}) is False

    def test_not_dict(self) -> None:
        assert _is_local_reference_step("str") is False

    def test_no_uses_key(self) -> None:
        assert _is_local_reference_step({"run": "echo hi"}) is False


class TestValidateWorkflow:
    """Tests for validate_workflow()."""

    def _write_workflow(self, path: Path, content: str) -> Path:
        path.write_text(content)
        return path

    def test_valid_checkout_first(self, tmp_path: Path) -> None:
        wf = self._write_workflow(
            tmp_path / "ci.yml",
            """
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/setup
""",
        )
        assert validate_workflow(wf) == []

    def test_checkout_missing_violation(self, tmp_path: Path) -> None:
        wf = self._write_workflow(
            tmp_path / "ci.yml",
            """
jobs:
  build:
    steps:
      - uses: ./.github/actions/setup
""",
        )
        violations = validate_workflow(wf)
        assert len(violations) == 1
        assert isinstance(violations[0], Violation)
        assert violations[0].job_name == "build"

    def test_no_jobs(self, tmp_path: Path) -> None:
        wf = self._write_workflow(tmp_path / "ci.yml", "name: empty\n")
        assert validate_workflow(wf) == []

    def test_large_file_skipped(self, tmp_path: Path) -> None:
        wf = tmp_path / "big.yml"
        wf.write_bytes(b"x" * (1_048_576 + 1))
        assert validate_workflow(wf) == []


class TestPiCliSetup:
    """Regression tests for installing the real Pi CLI in test environments."""

    def test_setup_pi_action_pins_real_npm_package(self) -> None:
        text = SETUP_PI_ACTION.read_text(encoding="utf-8")
        assert "actions/setup-node@" in text
        assert "node-version: 22.19.0" in text
        assert "npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.80.2" in text
        assert "pi --version" in text


class TestReleasingDoc:
    """Regression tests for the release documentation."""

    def test_releasing_doc_has_stranded_tag_recovery_section(self) -> None:
        """The recovery section the ::error:: annotation points at must exist."""
        doc = (REPO_ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
        assert "### Dispatch failed after tag push" in doc
        assert "gh workflow run release.yml -f tag=vX.Y.Z" in doc


class TestCollectWorkflowFiles:
    """Tests for collect_workflow_files()."""

    def test_finds_file(self, tmp_path: Path) -> None:
        f = tmp_path / "ci.yml"
        f.write_text("name: CI")
        result = collect_workflow_files([str(f)])
        assert f in result

    def test_finds_directory(self, tmp_path: Path) -> None:
        (tmp_path / "ci.yml").write_text("name: CI")
        (tmp_path / "release.yaml").write_text("name: Release")
        result = collect_workflow_files([str(tmp_path)])
        names = [p.name for p in result]
        assert "ci.yml" in names
        assert "release.yaml" in names

    def test_deduplicates(self, tmp_path: Path) -> None:
        f = tmp_path / "ci.yml"
        f.write_text("name: CI")
        result = collect_workflow_files([str(f), str(f)])
        assert len(result) == 1

    def test_missing_path_warns(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        collect_workflow_files([str(tmp_path / "nonexistent.yml")])
        captured = capsys.readouterr()
        assert "WARNING" in captured.err


class TestCLIEntryPoints:
    """Tests for check_workflow_inventory_main() and validate_workflow_checkout_main()."""

    def test_inventory_in_sync(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.ci.workflows import check_workflow_inventory_main

        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "README.md").write_text("| ci.yml | CI workflow |\n")
        monkeypatch.setattr(
            "sys.argv", ["hephaestus-check-workflow-inventory", "--repo-root", str(tmp_path)]
        )
        assert check_workflow_inventory_main() == 0

    def test_inventory_drift(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.ci.workflows import check_workflow_inventory_main

        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "README.md").write_text("| other.yml | Other |\n")
        monkeypatch.setattr(
            "sys.argv", ["hephaestus-check-workflow-inventory", "--repo-root", str(tmp_path)]
        )
        assert check_workflow_inventory_main() == 1

    def test_inventory_default_repo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hephaestus.ci.workflows import check_workflow_inventory_main

        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "README.md").write_text("| ci.yml | CI workflow |\n")

        monkeypatch.setattr("hephaestus.utils.helpers.get_repo_root", lambda: tmp_path)
        monkeypatch.setattr("sys.argv", ["hephaestus-check-workflow-inventory"])

        assert check_workflow_inventory_main() == 0

    def test_checkout_valid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from hephaestus.ci.workflows import validate_workflow_checkout_main

        wf = tmp_path / "ci.yml"
        wf.write_text(
            "jobs:\n  build:\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: ./.github/actions/setup\n"
        )
        monkeypatch.setattr("sys.argv", ["hephaestus-validate-workflow-checkout", str(wf)])
        assert validate_workflow_checkout_main() == 0
