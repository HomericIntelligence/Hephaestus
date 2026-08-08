"""Tests for hephaestus.ci.workflows."""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from hephaestus.ci import WorkflowValidationError, workflows as workflows_module
from hephaestus.ci.workflows import (
    _MAX_FILE_SIZE,
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
SETUP_PI_ACTION = REPO_ROOT / ".github/" / "actions" / "setup-pi-cli" / "action.yml"
CONTAINERFILE = REPO_ROOT / "ci" / "Containerfile"
NIGHTLY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nightly-tests.yml"


class TestCollectYmlFiles:
    """Tests for collect_yml_files()."""

    def test_finds_yml_files(self, tmp_path: Path) -> None:
        workflows = tmp_path / ".github/" / "workflows/"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "release.yml").write_text("name: Release")
        result = collect_yml_files(tmp_path)
        assert "ci.yml" in result
        assert "release.yml" in result

    def test_no_workflows_dir(self, tmp_path: Path) -> None:
        assert collect_yml_files(tmp_path) == set()

    def test_excludes_worktrees(self, tmp_path: Path) -> None:
        workflows = tmp_path / ".github/" / "workflows/"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        # Worktree path — create a worktrees subdir
        worktree_wf = tmp_path / "worktrees" / "branch" / ".github/" / "workflows/"
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
        workflows = tmp_path / ".github/" / "workflows/"
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


class TestWorkflowInventoryConfiguration:
    """Tests for workflow inventory configuration."""

    def test_workflow_inventory_hook_is_wired_in_precommit(self) -> None:
        from hephaestus.ci.precommit import load_precommit_config

        repos = load_precommit_config(
            Path(__file__).resolve().parents[3] / ".pre-commit-config.yaml"
        )
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
            == r"^(\.pre-commit-config\.yaml|\.github/" + r"workflows/(README\.md|.*\.yml))$"
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
        assert any("not performance" in option for option in addopts)


class TestNightlyTestsWorkflow:
    """Contracts for the scheduled high-cost functional-test lane."""

    def test_default_pytest_options_deselect_nightly_tests(self) -> None:
        """Developer and required-CI defaults must exclude the nightly marker."""
        config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
        assert any("not nightly" in option for option in addopts)


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
        assert _is_local_reference_step({"uses": "./.github/" + "workflows/reusable.yml"}) is True

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

    def test_large_file_raises(self, tmp_path: Path) -> None:
        wf = tmp_path / "big.yml"
        wf.write_bytes(b"x" * (_MAX_FILE_SIZE + 1))

        with pytest.raises(WorkflowValidationError) as caught:
            validate_workflow(wf)

        assert caught.value.errors[0].code == "oversized"


class TestWorkflowToolErrors:
    """Tests for direct validation failures that must not look clean."""

    def test_missing_pyyaml_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text("name: CI\n", encoding="utf-8")
        monkeypatch.setattr(workflows_module, "_yaml", None)

        with pytest.raises(WorkflowValidationError) as caught:
            validate_workflow(workflow)

        assert caught.value.errors[0].code == "dependency_unavailable"

    def test_file_stat_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text("name: CI\n", encoding="utf-8")
        real_stat = Path.stat

        def fail_workflow_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == workflow:
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fail_workflow_stat)

        with pytest.raises(WorkflowValidationError) as caught:
            validate_workflow(workflow)

        assert caught.value.errors[0].code == "stat_error"

    def test_read_failure_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text("name: CI\n", encoding="utf-8")
        real_open = Path.open

        def fail_workflow_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == workflow:
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_workflow_open)

        with pytest.raises(WorkflowValidationError) as caught:
            validate_workflow(workflow)

        assert caught.value.errors[0].code == "read_error"

    def test_decode_failure_raises(self, tmp_path: Path) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_bytes(b"name: \xff\n")

        with pytest.raises(WorkflowValidationError) as caught:
            validate_workflow(workflow)

        assert caught.value.errors[0].code == "decode_error"

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text("jobs: [\n", encoding="utf-8")

        with pytest.raises(WorkflowValidationError) as caught:
            validate_workflow(workflow)

        assert caught.value.errors[0].code == "yaml_parse"

    def test_empty_document_raises(self, tmp_path: Path) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text("", encoding="utf-8")

        with pytest.raises(WorkflowValidationError) as caught:
            validate_workflow(workflow)

        assert caught.value.errors[0].code == "empty_document"

    @pytest.mark.parametrize("contents", ["plain scalar\n", "- item\n"])
    def test_non_mapping_document_raises(self, tmp_path: Path, contents: str) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text(contents, encoding="utf-8")

        with pytest.raises(WorkflowValidationError) as caught:
            validate_workflow(workflow)

        assert caught.value.errors[0].code == "invalid_document"


class TestPiCliSetup:
    """Regression tests for installing the real Pi CLI in test environments."""

    def test_setup_pi_action_consumes_catalog_pinned_package(self) -> None:
        text = SETUP_PI_ACTION.read_text(encoding="utf-8")
        assert "actions/setup-node@" in text
        assert "node-version: 22.19.0" in text
        assert "pi_package_catalog.json" in text
        assert "@earendil-works/pi-coding-agent@0.80.2" not in text
        assert 'npm install -g --ignore-scripts "$pi_spec"' in text
        assert "pi --version" in text

    def test_container_and_nightly_lane_consume_the_catalog(self) -> None:
        container = CONTAINERFILE.read_text(encoding="utf-8")
        nightly = NIGHTLY_WORKFLOW.read_text(encoding="utf-8")

        assert "pi_package_catalog.json" in container
        assert "@earendil-works/pi-coding-agent@0.80.2" not in container
        assert "HEPHAESTUS_REQUIRE_PI_PACKAGE_SMOKE" in nightly

    def test_setup_pi_action_pins_setup_node_by_full_sha(self) -> None:
        """The composite action must not use a mutable setup-node tag."""
        text = SETUP_PI_ACTION.read_text(encoding="utf-8")
        match = re.search(r"uses:\s*actions/setup-node@([^\s]+)", text)
        assert match is not None
        assert re.fullmatch(r"[0-9a-f]{40}", match.group(1)) is not None


def test_releasing_doc_has_stranded_tag_recovery_section() -> None:
    """The recovery section the ::error:: annotation points at must exist."""
    doc = (REPO_ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
    assert "### Dispatch failed after tag push" in doc
    assert "gh workflow run release.yml -f tag=vX.Y.Z" in doc


def test_release_verifies_installed_wheel_before_publish() -> None:
    """The release gate verifies the built wheel before publishing it."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["build-and-publish"]["steps"]
    names = [step.get("name") for step in steps]

    verify_index = names.index("Verify canonical and installed versions")
    publish_index = names.index("Publish to PyPI")
    assert verify_index < publish_index

    command = steps[verify_index]["run"]
    assert 'VERIFY_ENV="build/version-verify"' in command
    assert "hephaestus-check-version-consistency" in command
    assert "--expected-version" in command


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

    def test_missing_path_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.yml"

        with pytest.raises(WorkflowValidationError) as caught:
            collect_workflow_files([str(missing)])

        assert caught.value.errors[0].code == "target_stat_error"
        assert caught.value.errors[0].target == missing


class TestCollectWorkflowFilesFailClosed:
    """Tests for discovery failures that must stop validation."""

    def test_directory_enumeration_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_scandir = os.scandir

        def fail_scandir(path: Any) -> Any:
            if Path(path) == tmp_path:
                raise PermissionError("denied")
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", fail_scandir)

        with pytest.raises(WorkflowValidationError) as caught:
            collect_workflow_files([str(tmp_path)])

        assert caught.value.errors[0].code == "directory_read_error"

    def test_unsupported_target_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "socket"
        real_stat = Path.stat

        def fake_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFIFO)
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fake_stat)

        with pytest.raises(WorkflowValidationError) as caught:
            collect_workflow_files([str(target)])

        assert caught.value.errors[0].code == "unsupported_target"

    def test_target_resolution_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text("name: CI\n", encoding="utf-8")
        real_resolve = Path.resolve

        def fail_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
            if path == workflow:
                raise OSError("cannot resolve")
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", fail_resolve)

        with pytest.raises(WorkflowValidationError) as caught:
            collect_workflow_files([str(workflow)])

        assert caught.value.errors[0].code == "target_resolve_error"


class TestWorkflowPublicApiCompatibility:
    """Successful public helpers retain their list-returning contracts."""

    def test_reexported_helpers_keep_list_returns_for_valid_inputs(self, tmp_path: Path) -> None:
        from hephaestus.ci import (
            collect_workflow_files as exported_collect,
            validate_workflow as exported_validate,
        )

        workflow = tmp_path / "ci.yml"
        workflow.write_text("name: CI\n", encoding="utf-8")

        assert exported_collect([str(workflow)]) == [workflow]
        assert exported_validate(workflow) == []


def _run_checkout_cli_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    paths: list[Path],
    allow_empty: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Run the checkout validator with JSON output and decode its response."""
    argv = ["hephaestus-validate-workflow-checkout", "--json"]
    if allow_empty:
        argv.append("--allow-empty")
    argv.extend(str(path) for path in paths)
    monkeypatch.setattr("sys.argv", argv)
    exit_code = workflows_module.validate_workflow_checkout_main()
    payload = json.loads(capsys.readouterr().out)
    return exit_code, payload


class TestCheckoutValidationToolErrorExitCodes:
    """Every tool-error category produces a nonzero CLI result."""

    def test_dependency_unavailable_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text("name: CI\n", encoding="utf-8")
        monkeypatch.setattr(workflows_module, "_yaml", None)

        exit_code, payload = _run_checkout_cli_json(monkeypatch, capsys, [workflow])

        assert exit_code == 1
        assert payload["tool_errors"][0]["code"] == "dependency_unavailable"

    def test_discovery_stat_failure_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code, payload = _run_checkout_cli_json(monkeypatch, capsys, [tmp_path / "missing.yml"])

        assert exit_code == 1
        assert payload["tool_errors"][0]["code"] == "target_stat_error"

    def test_workflow_stat_failure_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text("name: CI\n", encoding="utf-8")
        real_stat = Path.stat

        def fail_workflow_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == workflow:
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fail_workflow_stat)
        exit_code, payload = _run_checkout_cli_json(monkeypatch, capsys, [tmp_path])

        assert exit_code == 1
        assert payload["tool_errors"][0]["code"] == "stat_error"

    def test_unreadable_directory_returns_nonzero_with_allow_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        real_scandir = os.scandir

        def fail_scandir(path: Any) -> Any:
            if Path(path) == tmp_path:
                raise PermissionError("denied")
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", fail_scandir)
        exit_code, payload = _run_checkout_cli_json(
            monkeypatch, capsys, [tmp_path], allow_empty=True
        )

        assert exit_code == 1
        assert payload["tool_errors"][0]["code"] == "directory_read_error"

    def test_unsupported_target_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "socket"
        real_stat = Path.stat

        def fake_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFIFO)
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fake_stat)
        exit_code, payload = _run_checkout_cli_json(monkeypatch, capsys, [target])

        assert exit_code == 1
        assert payload["tool_errors"][0]["code"] == "unsupported_target"

    def test_target_resolution_failure_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text("name: CI\n", encoding="utf-8")
        real_resolve = Path.resolve

        def fail_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
            if path == workflow:
                raise OSError("cannot resolve")
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", fail_resolve)
        exit_code, payload = _run_checkout_cli_json(monkeypatch, capsys, [workflow])

        assert exit_code == 1
        assert payload["tool_errors"][0]["code"] == "target_resolve_error"

    @pytest.mark.parametrize(
        ("contents", "expected_code"),
        [
            pytest.param("x" * (_MAX_FILE_SIZE + 1), "oversized", id="oversized"),
            pytest.param("jobs: [\n", "yaml_parse", id="yaml_parse"),
            pytest.param("", "empty_document", id="empty_document"),
            pytest.param("plain scalar\n", "invalid_document", id="scalar_root"),
            pytest.param("- item\n", "invalid_document", id="list_root"),
        ],
    )
    def test_document_validation_failures_return_nonzero(
        self,
        tmp_path: Path,
        contents: str,
        expected_code: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        workflow = tmp_path / "ci.yml"
        if expected_code == "oversized":
            workflow.write_bytes(contents.encode("utf-8"))
        else:
            workflow.write_text(contents, encoding="utf-8")

        exit_code, payload = _run_checkout_cli_json(monkeypatch, capsys, [workflow])

        assert exit_code == 1
        assert payload["tool_errors"][0]["code"] == expected_code

    def test_read_failure_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_text("name: CI\n", encoding="utf-8")
        real_open = Path.open

        def fail_workflow_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == workflow:
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_workflow_open)
        exit_code, payload = _run_checkout_cli_json(monkeypatch, capsys, [workflow])

        assert exit_code == 1
        assert payload["tool_errors"][0]["code"] == "read_error"

    def test_decode_failure_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workflow = tmp_path / "ci.yml"
        workflow.write_bytes(b"name: \xff\n")
        exit_code, payload = _run_checkout_cli_json(monkeypatch, capsys, [workflow])

        assert exit_code == 1
        assert payload["tool_errors"][0]["code"] == "decode_error"


class TestEmptyWorkflowInventory:
    """Empty discovered inventories are policy failures unless opted out."""

    def test_empty_inventory_fails_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code, payload = _run_checkout_cli_json(monkeypatch, capsys, [tmp_path])

        assert exit_code == 1
        assert payload["policy_violations"][0]["code"] == "empty_inventory"
        assert payload["tool_error_count"] == 0

    def test_allow_empty_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code, payload = _run_checkout_cli_json(
            monkeypatch, capsys, [tmp_path], allow_empty=True
        )

        assert exit_code == 0
        assert payload["policy_violation_count"] == 0
        assert payload["tool_error_count"] == 0

    def test_allow_empty_does_not_hide_missing_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "missing.yml"
        exit_code, payload = _run_checkout_cli_json(
            monkeypatch, capsys, [missing], allow_empty=True
        )

        assert exit_code == 1
        assert payload["tool_errors"][0]["code"] == "target_stat_error"


def _write_mixed_failures(tmp_path: Path) -> tuple[Path, Path]:
    """Create one malformed workflow and one checkout-order violation."""
    malformed = tmp_path / "malformed.yml"
    malformed.write_text("jobs: [\n", encoding="utf-8")
    violating = tmp_path / "violating.yml"
    violating.write_text(
        "jobs:\n  build:\n    steps:\n      - uses: ./.github/actions/setup\n",
        encoding="utf-8",
    )
    return malformed, violating


class TestCheckoutValidationOutput:
    """Human and JSON output keep tool and policy failures distinct."""

    def test_json_separates_categories_and_preserves_legacy_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        malformed, violating = _write_mixed_failures(tmp_path)
        exit_code, payload = _run_checkout_cli_json(monkeypatch, capsys, [malformed, violating])

        assert exit_code == 1
        assert payload["status"] == "error"
        assert payload["exit_code"] == 1
        assert payload["files_checked"] == 2
        assert payload["violation_count"] == 1
        assert payload["policy_violation_count"] == 1
        assert payload["tool_error_count"] == 1
        assert payload["policy_violations"][0]["code"] == "checkout_order"
        assert payload["tool_errors"][0]["code"] == "yaml_parse"

    def test_human_output_labels_both_categories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        malformed, violating = _write_mixed_failures(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-validate-workflow-checkout",
                str(malformed),
                str(violating),
            ],
        )

        assert workflows_module.validate_workflow_checkout_main() == 1
        captured = capsys.readouterr()
        assert "TOOL ERROR [yaml_parse]" in captured.err
        assert str(malformed) in captured.err
        assert "POLICY VIOLATION [checkout_order]" in captured.out
        assert str(violating) in captured.out


class TestCLIEntryPoints:
    """Tests for check_workflow_inventory_main() and validate_workflow_checkout_main()."""

    def test_inventory_in_sync(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.ci.workflows import check_workflow_inventory_main

        workflows = tmp_path / ".github/" / "workflows/"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "README.md").write_text("| ci.yml | CI workflow |\n")
        monkeypatch.setattr(
            "sys.argv", ["hephaestus-check-workflow-inventory", "--repo-root", str(tmp_path)]
        )
        assert check_workflow_inventory_main() == 0

    def test_inventory_drift(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.ci.workflows import check_workflow_inventory_main

        workflows = tmp_path / ".github/" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "README.md").write_text("| other.yml | Other |\n")
        monkeypatch.setattr(
            "sys.argv", ["hephaestus-check-workflow-inventory", "--repo-root", str(tmp_path)]
        )
        assert check_workflow_inventory_main() == 1

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
