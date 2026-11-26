"""Tests for hephaestus.validation.stale_scripts."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.validation.stale_scripts import (
    _is_always_active,
    _script_referenced_by_command,
    _script_referenced_by_python_import,
    check_stale_scripts,
    find_stale_scripts,
    get_all_scripts,
    get_reference_targets,
    main,
)


def _write_script(scripts_dir: Path, name: str, content: str = "# placeholder\n") -> Path:
    p = scripts_dir / name
    p.write_text(content)
    return p


def _write_workflow(tmp_path: Path, name: str, content: str) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / name).write_text(content)


def _write_python_target(tmp_path: Path, name: str, content: str) -> Path:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    target = tests_dir / name
    target.write_text(content)
    return target


_COMMAND_FORMS: tuple[tuple[str, str], ...] = (
    ("python scripts/tool.py", "tool.py"),
    ("python3 ./scripts/tool.py", "tool.py"),
    ("bash scripts/tool.sh", "tool.sh"),
    ("sh scripts/tool.sh", "tool.sh"),
    ("source scripts/tool.sh", "tool.sh"),
    (". scripts/tool.sh", "tool.sh"),
    ("sbatch scripts/job.sbatch", "job.sbatch"),
    ("./scripts/tool.sh", "tool.sh"),
)

_IMPORT_FORMS: tuple[str, ...] = (
    "import scripts.shell.tool",
    "from scripts.shell import tool",
    "from scripts.shell.tool import run",
)


class TestIsAlwaysActive:
    """Tests for _is_always_active()."""

    def test_common_py(self) -> None:
        assert _is_always_active("common.py") is True

    def test_conftest(self) -> None:
        assert _is_always_active("conftest.py") is True

    def test_test_prefix(self) -> None:
        assert _is_always_active("test_foo.py") is True

    def test_regular_script(self) -> None:
        assert _is_always_active("bump_version.py") is False


class TestGetAllScripts:
    """Tests for get_all_scripts()."""

    def test_finds_py_files(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "a.py")
        _write_script(scripts_dir, "b.py")
        result = get_all_scripts(scripts_dir)
        assert "a.py" in result
        assert "b.py" in result

    def test_finds_sh_files(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("#!/bin/bash\n")
        result = get_all_scripts(scripts_dir)
        assert "run.sh" in result

    def test_finds_sbatch_files(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "job.sbatch").write_text("#!/bin/bash\n")
        assert "job.sbatch" in get_all_scripts(scripts_dir)

    def test_nested_scripts_use_scripts_relative_paths(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        nested = scripts_dir / "shell" / "lib"
        nested.mkdir(parents=True)
        (nested / "helpers.sh").write_text("")
        assert get_all_scripts(scripts_dir) == ["shell/lib/helpers.sh"]

    def test_ignores_dotfiles(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / ".hidden.py").write_text("")
        result = get_all_scripts(scripts_dir)
        assert ".hidden.py" not in result

    def test_empty_dir(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        assert get_all_scripts(scripts_dir) == []


class TestGetReferenceTargets:
    """Tests for get_reference_targets()."""

    def test_includes_workflow_yml(self, tmp_path: Path) -> None:
        _write_workflow(tmp_path, "ci.yml", "")
        targets = get_reference_targets(tmp_path)
        names = [t.name for t in targets]
        assert "ci.yml" in names

    def test_includes_justfile(self, tmp_path: Path) -> None:
        (tmp_path / "justfile").write_text("")
        targets = get_reference_targets(tmp_path)
        names = [t.name for t in targets]
        assert "justfile" in names

    def test_includes_precommit(self, tmp_path: Path) -> None:
        (tmp_path / ".pre-commit-config.yaml").write_text("")
        targets = get_reference_targets(tmp_path)
        names = [t.name for t in targets]
        assert ".pre-commit-config.yaml" in names

    def test_includes_scripts(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "helper.py")
        targets = get_reference_targets(tmp_path)
        names = [t.name for t in targets]
        assert "helper.py" in names

    def test_excludes_scripts_catalog(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "README.md").write_text("`orphan.py`")
        targets = get_reference_targets(tmp_path)
        assert scripts_dir / "README.md" not in targets

    def test_excludes_bytecode_caches(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        cache_dir = scripts_dir / "__pycache__"
        cache_dir.mkdir()
        bytecode = cache_dir / "orphan.cpython-313.pyc"
        bytecode.write_bytes(b"scripts/orphan.py")

        targets = get_reference_targets(tmp_path)

        assert bytecode not in targets

    def test_no_directories(self, tmp_path: Path) -> None:
        targets = get_reference_targets(tmp_path)
        assert all(t.is_file() for t in targets)


class TestFindStaleScripts:
    """Tests for find_stale_scripts()."""

    def test_referenced_script_not_stale(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "my_tool.py")
        _write_workflow(tmp_path, "ci.yml", "run: python scripts/my_tool.py")
        assert find_stale_scripts(tmp_path) == []

    def test_unreferenced_script_is_stale(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        result = find_stale_scripts(tmp_path)
        assert "orphan.py" in result

    def test_test_filename_substring_is_not_a_script_reference(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        tests_dir = tmp_path / "tests"
        scripts_dir.mkdir()
        tests_dir.mkdir()
        _write_script(scripts_dir, "show_prompt.py")
        (tests_dir / "test_structure.py").write_text("test_show_prompt.py\n")

        assert find_stale_scripts(tmp_path) == ["show_prompt.py"]

    def test_always_active_excluded(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "common.py")
        assert find_stale_scripts(tmp_path) == []

    def test_exclude_pattern(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "test_helper.py")
        # Without exclude: test_ is _is_always_active so already excluded
        # Let's use a non-test name
        _write_script(scripts_dir, "old_migration.py")
        result = find_stale_scripts(tmp_path, exclude_pattern="old_migration")
        assert "old_migration.py" not in result

    def test_cross_referenced_script_not_stale(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "helper.py")
        _write_script(scripts_dir, "runner.py", "import helper\n")
        result = find_stale_scripts(tmp_path)
        # helper.py should be found referenced in runner.py
        assert "helper.py" not in result

    def test_no_scripts_dir(self, tmp_path: Path) -> None:
        assert find_stale_scripts(tmp_path) == []

    def test_docs_reference(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "special.py")
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "guide.md").write_text("Run `python scripts/special.py` to start.")
        assert find_stale_scripts(tmp_path) == []

    def test_nested_script_command_uses_scripts_relative_path(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        nested_dir = scripts_dir / "shell"
        nested_dir.mkdir(parents=True)
        _write_script(nested_dir, "tool.sh")
        (tmp_path / "README.md").write_text("Run `bash scripts/shell/tool.sh`")
        assert find_stale_scripts(tmp_path) == []

    def test_test_string_literal_is_not_usage_evidence(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "tool.py")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_tool.py").write_text('path = "scripts/tool.py"\n')
        assert find_stale_scripts(tmp_path) == ["tool.py"]

    def test_catalog_entry_is_not_usage_evidence(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        (scripts_dir / "README.md").write_text("`orphan.py`")
        assert find_stale_scripts(tmp_path) == ["orphan.py"]

    def test_self_reference_does_not_count_as_usage(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py", "# scripts/orphan.py\n")
        assert find_stale_scripts(tmp_path) == ["orphan.py"]

    def test_same_basename_in_other_directory_does_not_activate(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        shell_dir = scripts_dir / "shell"
        other_dir = scripts_dir / "other"
        shell_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        _write_script(shell_dir, "tool.py")
        _write_script(other_dir, "tool.py")
        _write_script(shell_dir, "test_sibling.py", "import tool\n")

        stale = find_stale_scripts(tmp_path)

        assert "shell/tool.py" not in stale
        assert "other/tool.py" in stale

    def test_dotted_module_import_activates_exact_script(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        nested = scripts_dir / "shell"
        nested.mkdir(parents=True)
        _write_script(nested, "tool.py")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_use_tool.py").write_text("import scripts.shell.tool\n")
        assert find_stale_scripts(tmp_path) == []

    def test_from_parent_module_import_activates_exact_script(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        nested = scripts_dir / "shell"
        nested.mkdir(parents=True)
        _write_script(nested, "tool.py")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_use_tool.py").write_text("from scripts.shell import tool\n")
        assert find_stale_scripts(tmp_path) == []

    def test_from_module_import_activates_exact_script(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        nested = scripts_dir / "shell"
        nested.mkdir(parents=True)
        _write_script(nested, "tool.py")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_use_tool.py").write_text("from scripts.shell.tool import run\n")
        assert find_stale_scripts(tmp_path) == []

    def test_dotted_module_import_does_not_cross_directories(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        shell_dir = scripts_dir / "shell"
        other_dir = scripts_dir / "other"
        shell_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        _write_script(shell_dir, "tool.py")
        _write_script(other_dir, "tool.py")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_use_tool.py").write_text("import scripts.shell.tool\n")

        stale = find_stale_scripts(tmp_path)

        assert "shell/tool.py" not in stale
        assert "other/tool.py" in stale

    def test_sibling_import_activates_only_same_directory(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        shell_dir = scripts_dir / "shell"
        other_dir = scripts_dir / "other"
        shell_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        _write_script(shell_dir, "tool.py")
        _write_script(other_dir, "tool.py")
        _write_script(shell_dir, "test_sibling.py", "from tool import run\n")

        stale = find_stale_scripts(tmp_path)

        assert "shell/tool.py" not in stale
        assert "other/tool.py" in stale

    def test_shell_source_path_form_activates_nested_script(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        nested = scripts_dir / "shell"
        nested.mkdir(parents=True)
        (nested / "tool.sh").write_text("#!/bin/bash\n")
        _write_workflow(tmp_path, "ci.yml", "source scripts/shell/tool.sh\n")
        assert find_stale_scripts(tmp_path) == []

    def test_sbatch_path_form_activates_nested_script(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        nested = scripts_dir / "slurm"
        nested.mkdir(parents=True)
        (nested / "job.sbatch").write_text("#!/bin/bash\n")
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "guide.md").write_text("sbatch scripts/slurm/job.sbatch\n")
        assert find_stale_scripts(tmp_path) == []

    def test_sbatch_script_without_reference_is_stale(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        nested = scripts_dir / "slurm"
        nested.mkdir(parents=True)
        (nested / "job.sbatch").write_text("#!/bin/bash\n")
        assert find_stale_scripts(tmp_path) == ["slurm/job.sbatch"]


class TestCheckStaleScripts:
    """Tests for check_stale_scripts()."""

    def test_no_stale_returns_zero(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "tool.py")
        _write_workflow(tmp_path, "ci.yml", "run: python scripts/tool.py")
        assert check_stale_scripts(tmp_path) == 0

    def test_stale_warning_mode_returns_zero(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        assert check_stale_scripts(tmp_path, strict=False) == 0

    def test_stale_strict_mode_returns_one(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        assert check_stale_scripts(tmp_path, strict=True) == 1

    def test_verbose_output(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "tool.py")
        _write_workflow(tmp_path, "ci.yml", "python scripts/tool.py")
        check_stale_scripts(tmp_path, verbose=True)
        captured = capsys.readouterr()
        assert "Total scripts" in captured.out


class TestMain:
    """Tests for main() CLI entry point."""

    def test_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["hephaestus-check-stale-scripts", "--help"])
        assert main() == 0

    def test_no_stale_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "tool.py")
        _write_workflow(tmp_path, "ci.yml", "python scripts/tool.py")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-check-stale-scripts", "--repo-root", str(tmp_path)],
        )
        assert main() == 0

    def test_stale_strict_exits_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-check-stale-scripts",
                "--repo-root",
                str(tmp_path),
                "--strict",
            ],
        )
        assert main() == 1

    def test_stale_warning_mode_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-check-stale-scripts", "--repo-root", str(tmp_path)],
        )
        assert main() == 0


class TestScriptReferencedByCommand:
    """Tests for exact command operands and non-code mentions."""

    @pytest.mark.parametrize(("command", "script_path"), _COMMAND_FORMS)
    def test_command_form_marks_script_active(
        self, tmp_path: Path, command: str, script_path: str
    ) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = _write_script(scripts_dir, script_path)
        _write_workflow(tmp_path, "ci.yml", f"run: {command}\n")

        targets = get_reference_targets(tmp_path)

        assert _script_referenced_by_command(script_path, targets, script.resolve()) is True

    @pytest.mark.parametrize(("command", "script_path"), _COMMAND_FORMS)
    def test_command_form_through_find_stale_scripts(
        self, tmp_path: Path, command: str, script_path: str
    ) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, script_path)
        _write_workflow(tmp_path, "ci.yml", f"run: {command}\n")

        assert find_stale_scripts(tmp_path) == []

    def test_bare_path_in_prose_is_not_a_command(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = _write_script(scripts_dir, "tool.py")
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "guide.md").write_text("See scripts/tool.py for details.\n")

        targets = get_reference_targets(tmp_path)

        assert _script_referenced_by_command("tool.py", targets, script.resolve()) is False
        assert find_stale_scripts(tmp_path) == ["tool.py"]

    def test_python_comments_and_strings_are_not_commands(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = _write_script(scripts_dir, "tool.py")
        _write_python_target(
            tmp_path,
            "test_fake_usage.py",
            "# python scripts/tool.py\n"
            '"""python scripts/tool.py"""\n'
            'value = "python scripts/tool.py"\n',
        )

        targets = get_reference_targets(tmp_path)

        assert _script_referenced_by_command("tool.py", targets, script.resolve()) is False
        assert find_stale_scripts(tmp_path) == ["tool.py"]

    def test_shell_comment_is_not_a_command(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = _write_script(scripts_dir, "tool.py")
        (scripts_dir / "run.sh").write_text("#!/bin/bash\n# python scripts/tool.py\n")

        targets = get_reference_targets(tmp_path)

        assert _script_referenced_by_command("tool.py", targets, script.resolve()) is False

    def test_command_operand_must_match_exactly(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = _write_script(scripts_dir, "tool.py")
        _write_workflow(tmp_path, "ci.yml", "python scripts/tool.py.bak\n")

        targets = get_reference_targets(tmp_path)

        assert _script_referenced_by_command("tool.py", targets, script.resolve()) is False

    def test_nested_script_needs_nested_operand(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        nested_dir = scripts_dir / "shell"
        nested_dir.mkdir(parents=True)
        script = _write_script(nested_dir, "tool.sh")
        workflow = tmp_path / ".github" / "workflows" / "ci.yml"
        _write_workflow(tmp_path, "ci.yml", "bash scripts/tool.sh\n")
        targets = get_reference_targets(tmp_path)

        assert _script_referenced_by_command("shell/tool.sh", targets, script.resolve()) is False

        workflow.write_text("bash scripts/shell/tool.sh\n")
        assert _script_referenced_by_command("shell/tool.sh", targets, script.resolve()) is True


class TestScriptReferencedByPythonImport:
    """Tests for AST import resolution and non-code import mentions."""

    @pytest.mark.parametrize("statement", _IMPORT_FORMS)
    def test_import_spellings_resolve_exactly(self, tmp_path: Path, statement: str) -> None:
        scripts_dir = tmp_path / "scripts"
        shell_dir = scripts_dir / "shell"
        other_dir = scripts_dir / "other"
        shell_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        shell_script = _write_script(shell_dir, "tool.py")
        other_script = _write_script(other_dir, "tool.py")
        _write_python_target(tmp_path, "test_use_tool.py", statement + "\n")

        targets = get_reference_targets(tmp_path)

        assert (
            _script_referenced_by_python_import(
                "shell/tool.py", scripts_dir, targets, shell_script.resolve()
            )
            is True
        )
        assert (
            _script_referenced_by_python_import(
                "other/tool.py", scripts_dir, targets, other_script.resolve()
            )
            is False
        )

    @pytest.mark.parametrize("statement", _IMPORT_FORMS)
    def test_import_spellings_through_find_stale_scripts(
        self, tmp_path: Path, statement: str
    ) -> None:
        scripts_dir = tmp_path / "scripts"
        shell_dir = scripts_dir / "shell"
        other_dir = scripts_dir / "other"
        shell_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        _write_script(shell_dir, "tool.py")
        _write_script(other_dir, "tool.py")
        _write_python_target(tmp_path, "test_use_tool.py", statement + "\n")

        stale = find_stale_scripts(tmp_path)

        assert "shell/tool.py" not in stale
        assert "other/tool.py" in stale

    def test_comments_and_string_literals_are_not_imports(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = _write_script(scripts_dir, "orphan.py")
        _write_python_target(
            tmp_path,
            "test_fake_import.py",
            "# import scripts.orphan\n"
            '"""import scripts.orphan"""\n'
            'value = "import scripts.orphan"\n',
        )

        targets = get_reference_targets(tmp_path)

        assert (
            _script_referenced_by_python_import("orphan.py", scripts_dir, targets, script.resolve())
            is False
        )
        assert find_stale_scripts(tmp_path) == ["orphan.py"]

    def test_import_in_prose_is_not_an_import(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = _write_script(scripts_dir, "orphan.py")
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "guide.md").write_text("import scripts.orphan\n")

        targets = get_reference_targets(tmp_path)

        assert (
            _script_referenced_by_python_import("orphan.py", scripts_dir, targets, script.resolve())
            is False
        )
        assert find_stale_scripts(tmp_path) == ["orphan.py"]

    def test_nested_catalog_entry_is_not_usage_evidence(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        nested_dir = scripts_dir / "shell"
        nested_dir.mkdir(parents=True)
        _write_script(nested_dir, "tool.py")
        (scripts_dir / "README.md").write_text(
            "- `shell/tool.py`\n- Run `bash scripts/shell/tool.py`\n"
        )

        assert find_stale_scripts(tmp_path) == ["shell/tool.py"]
