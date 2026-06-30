"""Tests for hephaestus.scripts_lib.check_version_single_source.

This project uses hatch-vcs dynamic versioning. The checker validates that the
version has exactly one authority (git tags via hatch-vcs): no static
``[project].version``, ``version`` present in ``[project].dynamic``,
``[tool.hatch.version].source == "vcs"``, and no pixi ``[workspace].version``.
"""

from pathlib import Path

import pytest

from hephaestus.scripts_lib import check_version_single_source as mod
from hephaestus.scripts_lib.check_version_single_source import (
    check_pixi_no_version,
    check_pyproject_dynamic_version,
)

# A valid hatch-vcs pyproject.toml fragment (the real project's configuration).
VALID_PYPROJECT = (
    '[project]\nname = "mypkg"\ndynamic = ["version"]\n\n[tool.hatch.version]\nsource = "vcs"\n'
)


class TestCheckPyprojectDynamicVersion:
    """Tests for check_pyproject_dynamic_version()."""

    def test_returns_true_for_valid_hatch_vcs_config(self, tmp_path: Path) -> None:
        """Returns True when version is dynamic and hatch-vcs is the source."""
        (tmp_path / "pyproject.toml").write_text(VALID_PYPROJECT)
        assert check_pyproject_dynamic_version(tmp_path) is True

    def test_returns_false_when_pyproject_missing(self, tmp_path: Path) -> None:
        """Returns False when pyproject.toml does not exist."""
        assert check_pyproject_dynamic_version(tmp_path) is False

    def test_returns_false_when_static_version_reintroduced(self, tmp_path: Path) -> None:
        """Returns False when a static [project].version is present (regression for #435).

        The old regex-based checker would have matched an unrelated quoted string
        and reported a false PASS; the rewritten checker must FAIL here.
        """
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "mypkg"\n'
            'version = "1.2.3"\n'
            'dynamic = ["version"]\n\n'
            "[tool.hatch.version]\n"
            'source = "vcs"\n'
        )
        assert check_pyproject_dynamic_version(tmp_path) is False

    def test_returns_false_when_dynamic_missing_version(self, tmp_path: Path) -> None:
        """Returns False when [project].dynamic does not contain 'version'."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\ndynamic = []\n\n[tool.hatch.version]\nsource = "vcs"\n'
        )
        assert check_pyproject_dynamic_version(tmp_path) is False

    def test_returns_false_when_hatch_source_not_vcs(self, tmp_path: Path) -> None:
        """Returns False when [tool.hatch.version].source is not 'vcs'."""
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "mypkg"\n'
            'dynamic = ["version"]\n\n'
            "[tool.hatch.version]\n"
            'path = "mypkg/__init__.py"\n'
        )
        assert check_pyproject_dynamic_version(tmp_path) is False

    def test_does_not_match_entry_point_strings(self, tmp_path: Path) -> None:
        """A [project.scripts] entry point must not be mistaken for a version.

        Regression for #435: the old DOTALL regex captured
        'pkg.module:main' from [project.scripts] as the version.
        """
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "mypkg"\n'
            'dynamic = ["version"]\n\n'
            "[project.scripts]\n"
            'mypkg-cli = "mypkg.cli:main"\n\n'
            "[tool.hatch.version]\n"
            'source = "vcs"\n'
        )
        assert check_pyproject_dynamic_version(tmp_path) is True

    def test_prints_ok_on_success(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Prints OK message when configuration is valid."""
        (tmp_path / "pyproject.toml").write_text(VALID_PYPROJECT)
        check_pyproject_dynamic_version(tmp_path)
        assert "OK" in capsys.readouterr().out

    def test_prints_error_on_missing_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Prints ERROR message when pyproject.toml is missing."""
        check_pyproject_dynamic_version(tmp_path)
        assert "ERROR" in capsys.readouterr().out


class TestCheckPixiNoVersion:
    """Tests for check_pixi_no_version()."""

    def test_returns_true_when_pixi_absent(self, tmp_path: Path) -> None:
        """Returns True when pixi.toml does not exist."""
        assert check_pixi_no_version(tmp_path) is True

    def test_returns_true_when_no_workspace_section(self, tmp_path: Path) -> None:
        """Returns True when pixi.toml has no [workspace] section."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[dependencies]\npython = ">=3.10"\n')
        assert check_pixi_no_version(tmp_path) is True

    def test_returns_true_when_workspace_has_no_version(self, tmp_path: Path) -> None:
        """Returns True when [workspace] exists but has no version field."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[workspace]\nname = "myproject"\nchannels = ["conda-forge"]\n')
        assert check_pixi_no_version(tmp_path) is True

    def test_returns_false_when_workspace_has_version(self, tmp_path: Path) -> None:
        """Returns False when pixi.toml [workspace] contains a version field."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[workspace]\nname = "myproject"\nversion = "1.0.0"\n')
        assert check_pixi_no_version(tmp_path) is False

    def test_returns_true_when_version_in_other_section(self, tmp_path: Path) -> None:
        """Returns True when version appears in a section other than [workspace]."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text(
            '[workspace]\nname = "myproject"\n\n'
            '[feature.dev.pypi-dependencies]\nmypkg = {version = ">=1.0"}\n'
        )
        assert check_pixi_no_version(tmp_path) is True

    def test_prints_error_on_version_present(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Prints ERROR message when workspace version is found."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[workspace]\nversion = "0.1.0"\n')
        check_pixi_no_version(tmp_path)
        assert "ERROR" in capsys.readouterr().out

    def test_prints_ok_when_no_version(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Prints OK message when pixi.toml has no workspace version."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[workspace]\nname = "myproject"\n')
        check_pixi_no_version(tmp_path)
        assert "OK" in capsys.readouterr().out


class TestMain:
    """Tests for main()."""

    def test_returns_0_when_all_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns 0 for a valid hatch-vcs pyproject.toml and a versionless pixi.toml."""
        (tmp_path / "pyproject.toml").write_text(VALID_PYPROJECT)
        (tmp_path / "pixi.toml").write_text('[workspace]\nname = "myproject"\n')

        monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
        assert mod.main() == 0

    def test_returns_1_when_pyproject_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns 1 when pyproject.toml does not exist."""
        monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
        assert mod.main() == 1

    def test_returns_1_when_static_version_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns 1 when a static [project].version is reintroduced."""
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "mypkg"\n'
            'version = "1.0.0"\n'
            'dynamic = ["version"]\n\n'
            "[tool.hatch.version]\n"
            'source = "vcs"\n'
        )

        monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
        assert mod.main() == 1

    def test_returns_1_when_pixi_has_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns 1 when pixi.toml [workspace] has a version field."""
        (tmp_path / "pyproject.toml").write_text(VALID_PYPROJECT)
        (tmp_path / "pixi.toml").write_text('[workspace]\nversion = "1.0.0"\n')

        monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
        assert mod.main() == 1

    def test_returns_0_when_pixi_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns 0 when pixi.toml does not exist (only pyproject.toml)."""
        (tmp_path / "pyproject.toml").write_text(VALID_PYPROJECT)

        monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
        assert mod.main() == 0

    def test_passes_against_real_repo_pyproject(self) -> None:
        """The checker must PASS on the actual repository configuration."""
        assert mod.main() == 0
