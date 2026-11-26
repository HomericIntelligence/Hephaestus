"""Tests for the path-reference guard.

A registry module-path string or patch target that points at a deleted
package fails late; these tests hold the guard to its contract on synthetic
fixtures: resolvable references pass, root-only patch and string references
violate, import_module and console-script references must map exactly, and
external names are skipped.
"""

from __future__ import annotations

import pytest

from hephaestus.validation import path_references


@pytest.fixture
def repo(tmp_path):
    """Build a minimal synthetic repository tree."""
    (tmp_path / "hephaestus").mkdir(parents=True)
    (tmp_path / "hephaestus" / "__init__.py").write_text("")
    (tmp_path / "hephaestus" / "widget.py").write_text(
        "def main():\n    ...  # pragma: no cover\n\nvalue = 1\n"
    )
    (tmp_path / "hephaestus" / "nested").mkdir()
    (tmp_path / "hephaestus" / "nested" / "__init__.py").write_text("")
    (tmp_path / "hephaestus" / "nested" / "mod.py").write_text(
        "def render():\n    ...  # pragma: no cover\n"
    )
    (tmp_path / "hephaestus" / "other").mkdir()
    (tmp_path / "hephaestus" / "other" / "__init__.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / ".pre-commit-config.yaml").write_text("files: ^(hephaestus|tests)/.*\\.py$\n")
    return tmp_path


def _write(repo, relative: str, contents: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")


def _kinds(findings):
    return {finding.kind for finding in findings}


class TestPatchTargets:
    """patch, patch.object, and monkeypatch.setattr string targets."""

    def test_attribute_tail_resolves(self, repo) -> None:
        _write(
            repo,
            "tests/test_widget.py",
            'from unittest.mock import patch\n\npatch("hephaestus.widget.value").start()\n',
        )
        assert path_references.find_violations(repo) == []
        assert (
            path_references.resolve_module(repo, "hephaestus.widget.value") == "hephaestus.widget"
        )

    def test_missing_module_violates(self, repo) -> None:
        _write(
            repo,
            "tests/test_widget.py",
            'from unittest.mock import patch\n\npatch("hephaestus.missing_mod.target").start()\n',
        )
        findings = path_references.find_violations(repo)
        assert _kinds(findings) == {"patch-target"}
        assert any("hephaestus.missing_mod.target" in finding.detail for finding in findings)

    def test_monkeypatch_setattr_violates(self, repo) -> None:
        _write(
            repo,
            "tests/test_widget.py",
            'def test_env(monkeypatch):\n    monkeypatch.setattr("hephaestus.wrong_pkg.tok", 1)\n',
        )
        assert _kinds(path_references.find_violations(repo)) == {"patch-target"}

    def test_patch_object_violates(self, repo) -> None:
        _write(
            repo,
            "tests/test_widget.py",
            'from unittest.mock import patch\n\npatch.object("hephaestus.missing_mod", "run")\n',
        )
        assert _kinds(path_references.find_violations(repo)) == {"patch-target"}

    def test_external_targets_are_skipped(self, repo) -> None:
        _write(
            repo,
            "tests/test_widget.py",
            'from unittest.mock import patch\n\npatch("os.path.join")\n'
            'def test_ext(monkeypatch):\n    monkeypatch.setattr("builtins.input", lambda: "")\n',
        )
        assert path_references.find_violations(repo) == []

    def test_fstring_target_is_skipped(self, repo) -> None:
        """f-string targets are dynamic and out of declarative reach."""
        _write(repo, "tests/test_widget.py", 'patch(f"{name}.value").start()\n')
        assert path_references.find_violations(repo) == []


class TestImportModuleReferences:
    """importlib.import_module literal arguments name the module exactly."""

    def test_non_module_tail_violates(self, repo) -> None:
        _write(
            repo,
            "hephaestus/registry.py",
            'import importlib\n\nfn = importlib.import_module("hephaestus.widget.value")\n',
        )
        findings = path_references.find_violations(repo)
        assert _kinds(findings) == {"module-attribute"}
        assert any("hephaestus.widget.value" in finding.detail for finding in findings)

    def test_module_name_resolves(self, repo) -> None:
        _write(
            repo,
            "hephaestus/registry.py",
            'import importlib\n\nmod = importlib.import_module("hephaestus.widget")\n',
        )
        assert path_references.find_violations(repo) == []

    def test_external_name_is_skipped(self, repo) -> None:
        _write(
            repo,
            "hephaestus/registry.py",
            'import importlib\n\nmod = importlib.import_module("yaml")\n',
        )
        assert path_references.find_violations(repo) == []


class TestModuleStrings:
    """Repo-prefixed string constants such as the environment-registry readers."""

    def test_root_only_string_violates(self, repo) -> None:
        _write(repo, "hephaestus/registry.py", '_READER = "hephaestus.missing.reader"\n')
        assert _kinds(path_references.find_violations(repo)) == {"module-string"}

    def test_exempt_marker_skips_constant(self, repo) -> None:
        _write(
            repo,
            "hephaestus/registry.py",
            "# path-reference-exempt: entry-point group, not a module\n"
            '_ENTRY_POINT_GROUP = "hephaestus.missing.reader"\n',
        )
        assert path_references.find_violations(repo) == []

    def test_package_module_path_resolves(self, repo) -> None:
        _write(
            repo,
            "hephaestus/registry.py",
            '_READER = "hephaestus.nested.mod.render"\n',
        )
        assert path_references.find_violations(repo) == []

    def test_call_arg_is_not_double_counted(self, repo) -> None:
        _write(
            repo,
            "hephaestus/registry.py",
            'import importlib\n\nmod = importlib.import_module("hephaestus.missing")\n',
        )
        findings = path_references.find_violations(repo)
        assert _kinds(findings) == {"module-attribute"}
        assert len(findings) == 1


class TestConsoleScripts:
    """[project.scripts] entries must name an importable module with the attr."""

    def test_violations(self, repo) -> None:
        _write(
            repo,
            "pyproject.toml",
            "[project.scripts]\n"
            'good = "hephaestus.widget:main"\n'
            'missing-module = "hephaestus.gone:main"\n'
            'missing-attr = "hephaestus.widget:absent"\n',
        )
        findings = path_references.find_violations(repo)
        assert _kinds(findings) == {"console-script"}
        assert len(findings) == 2

    def test_ok(self, repo) -> None:
        _write(repo, "pyproject.toml", '[project.scripts]\ngood = "hephaestus.widget:main"\n')
        assert path_references.find_violations(repo) == []


class TestHookCoverage:
    """Every subpackage needs a pre-commit files: pattern that names it."""

    def test_violation(self, repo) -> None:
        _write(repo, ".pre-commit-config.yaml", "files: ^hephaestus/nested/.*\\.py$\n")
        findings = path_references.find_violations(repo)
        assert _kinds(findings) == {"hook-coverage"}
        assert any("hephaestus/other" in finding.detail for finding in findings)

    def test_broad_pattern_covers_all(self, repo) -> None:
        _write(repo, ".pre-commit-config.yaml", "files: ^hephaestus/.*$\n")
        assert path_references.find_violations(repo) == []

    def test_missing_config_reports_all(self, repo) -> None:
        (repo / ".pre-commit-config.yaml").unlink()
        findings = path_references.find_violations(repo)
        assert _kinds(findings) == {"hook-coverage"}
        assert len(findings) == 2


class TestResolutionHelpers:
    """Unit-level checks of the dotted-name resolution helpers."""

    def test_has_top_level_attr_forms(self, repo) -> None:
        file = repo / "hephaestus" / "widget.py"
        assert path_references._has_top_level_attr(file, "main")
        assert not path_references._has_top_level_attr(file, "absent")

    def test_reexported_main_counts(self, repo) -> None:
        _write(repo, "hephaestus/cli.py", "from hephaestus.widget import main\n")
        assert path_references._has_top_level_attr(repo / "hephaestus" / "cli.py", "main")

    def test_dotted_to_file_package_and_module(self, repo) -> None:
        assert path_references.dotted_to_file(repo, "hephaestus.widget") is not None
        assert path_references.dotted_to_file(repo, "hephaestus.nested") is not None
        assert path_references.dotted_to_file(repo, "hephaestus.gone") is None
        assert path_references.dotted_to_file(repo, "yaml") is None

    def test_resolve_module_rules(self, repo) -> None:
        assert path_references.module_cannot_exist(repo, "hephaestus.missing_mod.target")
        assert not path_references.module_cannot_exist(repo, "hephaestus.widget.value")
        assert (
            path_references.resolve_module(repo, "hephaestus.widget.value") == "hephaestus.widget"
        )
        assert path_references.resolve_module(repo, "yaml") is None
